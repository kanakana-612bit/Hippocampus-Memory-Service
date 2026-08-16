from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any


SEGMENTATION_VERSION = "conversation_segmentation_v1"
DEFAULT_SESSION_GAP_MINUTES = 90
DEFAULT_CONTEXT_TOKENS = 12000
DEFAULT_OVERLAP_TURNS = 2
DEFAULT_TOPIC_BOUNDARY_THRESHOLD = 0.68

TRANSITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "explicit_topic_transition_ja",
        re.compile(
            r"^\s*(?:ところで|さて(?:[、,])?|話(?:は|を)?変わる(?:けど|が|と)|"
            r"話がそれる(?:けど|が|と)|余談(?:だけど|ですが|になるが)|別件(?:だけど|ですが|で))",
            re.I,
        ),
    ),
    (
        "explicit_topic_transition_en",
        re.compile(
            r"^\s*(?:by the way|anyway|on another topic|changing the subject|as an aside)\b",
            re.I,
        ),
    ),
)


def event_time(event: dict[str, Any]) -> datetime | None:
    value = event.get("event_time") or event.get("created_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def event_actor(event: dict[str, Any]) -> str:
    return str(event.get("actor_role") or event.get("role") or "unknown").lower()


def estimate_text_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate for mixed Japanese and Latin text."""
    ascii_count = sum(1 for char in text if ord(char) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, math.ceil(ascii_count / 4) + non_ascii_count)


def estimate_event_tokens(event: dict[str, Any]) -> int:
    content = str(event.get("content") or "")
    return 18 + estimate_text_tokens(content)


def transition_signals(content: str) -> list[str]:
    return [name for name, pattern in TRANSITION_PATTERNS if pattern.search(content or "")]


def deterministic_session_boundaries(
    events: list[dict[str, Any]],
    *,
    gap_minutes: int = DEFAULT_SESSION_GAP_MINUTES,
) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    previous_user_time: datetime | None = None
    for index, event in enumerate(events):
        if event_actor(event) != "user":
            continue
        current_time = event_time(event)
        if previous_user_time is not None and current_time is not None:
            gap_seconds = (current_time - previous_user_time).total_seconds()
            if gap_seconds >= max(1, gap_minutes) * 60:
                boundaries.append(
                    {
                        "boundary_type": "session",
                        "before_event_id": str(events[index - 1]["id"]) if index else None,
                        "after_event_id": str(event["id"]),
                        "boundary_time": str(event.get("event_time") or event.get("created_at") or ""),
                        "detection_source": "deterministic",
                        "confidence": 0.99,
                        "reason": f"user_activity_gap_{round(gap_seconds / 60.0, 3)}_minutes",
                        "signals": ["user_activity_gap"],
                        "previous_topic": None,
                        "next_topic": None,
                    }
                )
        if current_time is not None:
            previous_user_time = current_time
    return boundaries


def explicit_topic_candidates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if index == 0 or event_actor(event) != "user":
            continue
        signals = transition_signals(str(event.get("content") or ""))
        if not signals:
            continue
        candidates.append(
            {
                "boundary_type": "topic",
                "before_event_id": str(events[index - 1]["id"]),
                "after_event_id": str(event["id"]),
                "boundary_time": str(event.get("event_time") or event.get("created_at") or ""),
                "detection_source": "rule_candidate",
                "confidence": 0.72,
                "reason": "explicit_transition_expression",
                "signals": signals,
                "previous_topic": None,
                "next_topic": None,
            }
        )
    return candidates


def split_events(
    events: list[dict[str, Any]],
    *,
    boundary_after_event_ids: set[str],
) -> list[list[dict[str, Any]]]:
    if not events:
        return []
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event["id"])
        if current and event_id in boundary_after_event_ids:
            groups.append(current)
            current = []
        current.append(event)
    if current:
        groups.append(current)
    return groups


def token_chunks(
    events: list[dict[str, Any]],
    *,
    max_tokens: int = DEFAULT_CONTEXT_TOKENS,
    overlap_turns: int = DEFAULT_OVERLAP_TURNS,
) -> list[dict[str, Any]]:
    if not events:
        return []
    budget = max(256, int(max_tokens))
    overlap = max(0, int(overlap_turns))
    primary_budget = max(128, int(budget * 0.8)) if overlap else budget
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(events):
        end = start
        primary_tokens = 0
        while end < len(events):
            next_tokens = estimate_event_tokens(events[end])
            if end > start and primary_tokens + next_tokens > primary_budget:
                break
            primary_tokens += next_tokens
            end += 1
        if end == start:
            end += 1
        context_start = max(0, start - overlap)
        context_end = min(len(events), end + overlap)
        context_tokens = sum(
            estimate_event_tokens(item) for item in events[context_start:context_end]
        )
        while context_tokens > budget and (context_start < start or context_end > end):
            left_cost = (
                estimate_event_tokens(events[context_start]) if context_start < start else -1
            )
            right_cost = (
                estimate_event_tokens(events[context_end - 1]) if context_end > end else -1
            )
            if left_cost >= right_cost and context_start < start:
                context_tokens -= left_cost
                context_start += 1
            elif context_end > end:
                context_tokens -= right_cost
                context_end -= 1
        context_events = events[context_start:context_end]
        chunks.append(
            {
                "events": context_events,
                "primary_event_ids": [str(item["id"]) for item in events[start:end]],
                "context_event_ids": [str(item["id"]) for item in context_events],
                "estimated_tokens": context_tokens,
                "budget_exceeded": context_tokens > budget,
            }
        )
        start = end
    return chunks


def deterministic_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"
