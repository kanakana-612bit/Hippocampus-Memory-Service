"""Shared initial/daily normalization. Model output never supplies original text."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo

from .common import Invalid, identifier, strict_json, text_digest
from .contracts import TimeInterpretation

ROLES = {"agent": "assistant", **{r: r for r in ("user", "assistant", "system", "developer", "tool")}}
POLICY = "normalization.v1"


def datetime_value(value, timezone_name):
    if not isinstance(value, str):
        raise Invalid("Datetime must be a string")
    original = value
    value = value.replace("/", "-").replace("Z", "+00:00")
    if not re.search(r"[T ]\d{2}:\d{2}", value):
        raise Invalid("A point datetime requires an explicit hour and minute")
    fraction = re.search(r":\d{2}\.(\d+)", value)
    if fraction and len(fraction[1]) > 6:
        raise Invalid("Sub-microsecond input requires explicit precision support")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Invalid("Uninterpretable datetime; review required") from exc
    precision = "fractional_" + str(len(fraction[1])) if fraction else "second" if re.search(r"\d:\d\d:\d\d", value) else "minute"
    if parsed.tzinfo is None:
        if not timezone_name:
            raise Invalid("Timezone unresolved; no machine-local fallback")
        try:
            zone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise Invalid("Unknown explicit timezone") from exc
        alternatives = [parsed.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
        valid = [x for x in alternatives if x.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == parsed]
        if not valid or len({x.utcoffset() for x in valid}) > 1:
            raise Invalid("Ambiguous or nonexistent local datetime; review required")
        parsed = valid[0]
        source = "explicit_import_timezone"
    else:
        source = "original_offset"
    return parsed, {"original_datetime": original, "original_precision": precision,
                    "timezone": timezone_name or str(parsed.tzinfo), "timezone_source": source}


def normalize(raw: bytes, *, source_id: str, epoch: str, import_timezone: str | None = None):
    try:
        document = strict_json(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise Invalid("Raw must be UTF-8 JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list) or not document["messages"]:
        raise Invalid("A nonempty ordered messages array is required")
    messages = document["messages"]
    tz = document.get("timezone") or import_timezone
    tz_source = "original_document_timezone" if document.get("timezone") else "explicit_import_timezone"
    duration = document.get("total_session_time_minutes")
    if duration is None or duration == "unknown":
        duration = None
    elif isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        raise Invalid("Duration must be finite nonnegative minutes or unknown")
    start = document.get("session_start_datetime")
    period = document.get("approximate_period")
    if period is not None and (not isinstance(period, str) or not period.strip()):
        raise Invalid("Approximate period must be a nonempty string")
    uniform_start = datetime_value(start, tz) if start is not None and duration is not None else None
    if uniform_start and uniform_start[1]["timezone_source"] == "explicit_import_timezone":
        uniform_start[1]["timezone_source"] = tz_source
    events, warnings = [], []
    turn = None
    previous_point = None
    pending_tools, turn_tool_ids = set(), set()
    for sequence, message in enumerate(messages, 1):
        if not isinstance(message, dict) or message.get("role") not in ROLES or not isinstance(message.get("text"), str):
            raise Invalid("Unknown role or invalid message; input was not repaired")
        role = ROLES[message["role"]]
        status = message.get("status", "completed")
        if status not in ("completed", "interrupted", "in_progress"):
            raise Invalid("Unknown capture status")
        if role == "user" or turn is None:
            if pending_tools:
                raise Invalid("Tool results are missing before the next user turn")
            turn = identifier("turn", epoch, source_id, sequence)
            turn_tool_ids = set()
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or (calls and role != "assistant"):
            raise Invalid("Tool calls must be an assistant call list")
        for call in calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            if not isinstance(call_id, str) or not call_id or call_id in turn_tool_ids:
                raise Invalid("Invalid or duplicate tool call id within a turn")
            pending_tools.add(call_id)
            turn_tool_ids.add(call_id)
        if role == "tool":
            if message.get("tool_call_id") not in pending_tools:
                raise Invalid("Tool result has no corresponding pending call in the same turn")
            pending_tools.remove(message["tool_call_id"])
        event_id = identifier("event", epoch, source_id, sequence)
        raw_datetime = message.get("datetime")
        if raw_datetime is not None:
            point, metadata = datetime_value(raw_datetime, tz)
            if metadata["timezone_source"] == "explicit_import_timezone":
                metadata["timezone_source"] = tz_source
            temporal = TimeInterpretation(time_quality="exact", event_time=point.isoformat(), **metadata)
        elif uniform_start is not None:
            micros = (Decimal(sequence - 1) * Decimal(str(duration)) * 60_000_000 / max(1, len(messages) - 1)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
            point = uniform_start[0] + timedelta(microseconds=int(micros))
            temporal = TimeInterpretation(time_quality="estimated_uniform", event_time=point.isoformat(),
                timezone=uniform_start[1]["timezone"], timezone_source=uniform_start[1]["timezone_source"],
                relative_offset_seconds=float(micros / 1_000_000), method="uniform_session", estimation_precision="coarse_estimate",
                evidence={"session_start_datetime": start, "total_session_time_minutes": duration,
                          "session_message_count": len(messages), "formula": "(sequence-1)*D/(N-1); N=1 -> 0", "rounding": "microsecond_half_even"})
        elif period is not None:
            temporal = TimeInterpretation(time_quality="approximate_range", approximate_period=period,
                relative_offset_seconds=(sequence - 1) * 120, method="assumed_two_minutes", estimation_precision="range_only")
        else:
            temporal = TimeInterpretation(time_quality="unknown")
        if temporal.event_time:
            point = datetime.fromisoformat(temporal.event_time)
            if previous_point and point < previous_point:
                warnings.append({"code": "nonmonotonic_time", "sequence": sequence, "requires_review": True})
            previous_point = point
        if raw_datetime is not None and uniform_start is not None:
            expected = uniform_start[0] + timedelta(seconds=(sequence - 1) * duration * 60 / max(1, len(messages) - 1))
            if datetime.fromisoformat(temporal.event_time) != expected:
                warnings.append({"code": "exact_uniform_conflict", "sequence": sequence, "requires_review": True})
        events.append({"id": event_id, "role": role, "content": message["text"], "sequence": sequence,
            "source_id": source_id, "turn_id": turn, "status": status,
            "source_digest": text_digest(message["text"]), "raw_role": message["role"],
            "raw_message": message, "time": temporal.model_dump(),
            "tool_call_id": message.get("tool_call_id"), "tool_calls": message.get("tool_calls", [])})
    if pending_tools and not any(e["status"] == "in_progress" for e in events if e["turn_id"] == turn):
        raise Invalid("Completed source has unresolved tool calls")
    return {"events": events, "warnings": warnings,
            "session_metadata": {key: value for key, value in document.items() if key != "messages"}, "policy_version": POLICY}
