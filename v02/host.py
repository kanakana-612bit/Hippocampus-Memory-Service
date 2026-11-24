"""Canonical HTTP client and pure host adapters; no legacy API fallback."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from openwebui_adapter import dialogue_payload
from .contracts import canonical, digest


class Client:
    def __init__(self, url: str, token: str, principal: str, timeout: float = 8):
        if not url or not token or not principal:
            raise ValueError("Canonical client URL, credential and principal are required")
        self.url = url.rstrip("/") + "/api/v0.2"
        self.token = token
        self.principal = principal
        self.timeout = timeout

    def request(self, method, route, payload=None, *, timeout=None):
        request = urllib.request.Request(self.url + route, method=method,
            data=canonical(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout if timeout is not None else self.timeout) as response:
            return json.load(response)


def canonical_dialogue(chat_id, chat, user_id, *, active_leaf=None, logical_actor_id="assistant:primary"):
    adapted = dialogue_payload(chat_id, chat, user_id, logical_actor_id=logical_actor_id, active_leaf=active_leaf)
    messages = []
    for event in adapted["messages"]:
        meta = event["meta"]
        messages.append({"id": event["id"], "role": event["role"], "content": event["content"],
                         "actor_id": event.get("actor_id"), "logical_actor_id": meta.get("logical_actor_id"),
                         "model_provenance": meta.get("model_provenance", {}),
                         "event_time": event.get("event_time"), "source_message_id": meta["source_message_id"]})
    return {"conversation_id": chat_id, "messages": messages, "active_event_ids": adapted["active_event_ids"]}


def final_input(form_data, context_text, packet_digest, capacity=None):
    """Measure without truncation. Only an explicitly supplied model capacity can block."""
    payload = {k: form_data.get(k) for k in ("messages", "tools", "response_format", "max_tokens", "params", "model")}
    input_bytes = len(canonical({k: payload[k] for k in ("messages", "tools", "response_format")}).encode())
    settings = capacity or {}
    limit = settings.get("context_window")
    reserve = settings.get("generation_reserve", (form_data.get("params") or {}).get("max_tokens") or form_data.get("max_tokens"))
    measured = settings.get("measured_input_tokens")
    overhead = settings.get("template_overhead_tokens")
    for value in (limit, reserve, measured, overhead):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("Model capacity values must be nonnegative integers")
    if limit == 0:
        raise ValueError("Model context window must be positive")
    estimate = measured if measured is not None else input_bytes
    total = estimate + (reserve or 0) + (overhead or 0)
    # An estimate exceeding capacity is not proof that the actual token input exceeds
    # capacity. Only measured input can block; unknown tokenizers stay observable.
    over = bool(limit is not None and measured is not None and total > limit)
    present = bool(context_text) and any(m.get("role") == "system" and isinstance(m.get("content"), str)
        and context_text in m["content"] for m in form_data.get("messages", []))
    status = "blocked_capacity" if over else "retained" if present else "empty_context" if not context_text else "not_present"
    return {"stage": "host_finalized", "status": status, "request_digest": digest(payload),
            "packet_digest": packet_digest if status == "retained" else None,
            "metrics": {"input_utf8_bytes": input_bytes, "input_tokens": estimate,
                        "estimator": "provider_measurement" if measured is not None else "utf8_bytes_upper_bound",
                        "context_window": limit, "generation_reserve": reserve, "template_overhead": overhead,
                        "estimated_total_tokens": total, "input_over_budget": over,
                        "estimated_overflow_possible": bool(limit is not None and total > limit),
                        "model_id": str(form_data.get("model") or "unknown"),
                        "capacity_known": limit is not None, "hippocampus_context_cap": None}}
