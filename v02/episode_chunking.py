"""Pure processing boundaries based on user inactivity, never semantic windows.

Active path order is authoritative. Assistant/tool replies stay with the user
turn. Processing limits may split between turns; one oversized turn is retained
whole. The caller owns authorization, event immutability and clock selection.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Literal

from .contracts import timestamp
from .episode_contracts import ChunkPolicy, EpisodeContract


class PlannedChunk(EpisodeContract):
    event_ids: tuple[str, ...]
    reason: Literal["idle", "processing_limit"]
    started_at: str
    ended_at: str
    eligible_at: str
    last_user_at: str | None
    character_count: int


class ChunkPlan(EpisodeContract):
    sealed: tuple[PlannedChunk, ...]
    pending_event_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def _stamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("A timezone is required")
        return value.astimezone(timezone.utc)
    return timestamp(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value is not None else None


def _event_time(record, data):
    for field, value in (
        ("event_time", data.get("event_time")),
        ("received_at", data.get("received_at") or record.get("received_at")),
        ("created_at", record.get("created_at")),
    ):
        if value:
            try:
                return _stamp(value), field
            except (TypeError, ValueError, OverflowError):
                pass
    raise ValueError("A source event needs a valid event_time or recorded receipt time")


def plan_chunks(
    events: Iterable[Mapping] | Mapping[str, Mapping],
    active_event_ids: Iterable[str],
    *,
    now: str | datetime,
    already_sealed_event_ids: Iterable[str] = (),
    response_in_progress: bool = False,
    policy: ChunkPolicy | Mapping | None = None,
) -> ChunkPlan:
    """Plan closed chunks without writing, summarizing, or touching source text.

    ``now`` is injected by the trusted service, not an API request. The last
    user event starts the inactivity timer; non-user replies never reset it.
    A later user gap can close historical chunks even while the newest response
    is still being generated. A sealed prefix must be contiguous on this path.
    """
    policy = ChunkPolicy.model_validate(policy or {})
    clock = _stamp(now)
    path_ids = tuple(active_event_ids)
    if len(path_ids) != len(set(path_ids)):
        raise ValueError("The active path contains duplicate events")
    sealed_ids = tuple(already_sealed_event_ids)
    if len(sealed_ids) != len(set(sealed_ids)):
        raise ValueError("Sealed prefix contains duplicate events")
    if set(sealed_ids) != set(path_ids[:len(sealed_ids)]):
        raise ValueError("Already sealed events must cover a contiguous active path prefix")
    if isinstance(events, Mapping):
        by_id = dict(events)
    else:
        records = tuple(events)
        by_id = {record["id"]: record for record in records}
        if len(by_id) != len(records):
            raise ValueError("Duplicate source event records")
    if not set(path_ids) <= by_id.keys():
        raise ValueError("The active path contains unavailable source events")

    warnings = []
    ordered = []
    previous_time = None
    for event_id in path_ids:
        record = by_id[event_id]
        if record.get("id", event_id) != event_id:
            raise ValueError("Source record identity differs from its path identity")
        data = record.get("data", record)
        if data.get("role") not in {"user", "assistant", "tool", "system"}:
            raise ValueError("Unknown source event role")
        content = data.get("content", data.get("raw_text"))
        if not isinstance(content, str):
            raise ValueError("Source event content must be text")
        event_time, time_source = _event_time(record, data)
        if time_source != "event_time":
            warnings.append(f"receipt_time_fallback:{event_id}:{time_source}")
        if previous_time is not None and event_time < previous_time:
            # Clock skew never reorders raw source events or creates a false gap.
            warnings.append(f"nonmonotonic_time_clamped:{event_id}")
            event_time = previous_time
        previous_time = event_time
        if event_time > clock:
            warnings.append(f"future_event_time:{event_id}")
        ordered.append((event_id, data["role"], len(content), event_time))

    pending = []
    chunks = []
    last_user = None
    characters = 0
    idle = timedelta(seconds=policy.inactivity_seconds)

    def close(reason, eligible_at):
        nonlocal pending, last_user, characters
        if not pending:
            return
        chunks.append(PlannedChunk(
            event_ids=tuple(row[0] for row in pending), reason=reason,
            started_at=_iso(pending[0][3]), ended_at=_iso(pending[-1][3]),
            eligible_at=_iso(eligible_at), last_user_at=_iso(last_user),
            character_count=characters,
        ))
        pending, last_user, characters = [], None, 0

    for row in ordered[len(sealed_ids):]:
        event_id, role, count, event_time = row
        if role == "user" and pending:
            deadline = last_user + idle if last_user is not None else None
            if (deadline is not None and event_time >= deadline
                    and max(deadline, pending[-1][3]) <= clock):
                close("idle", max(deadline, pending[-1][3]))
            else:
                over_events = policy.max_events is not None and len(pending) + 1 > policy.max_events
                over_characters = (policy.max_characters is not None
                                   and characters + count > policy.max_characters)
                if (over_events or over_characters) and pending[-1][3] <= clock:
                    close("processing_limit", pending[-1][3])
        pending.append(row)
        characters += count
        if role == "user":
            last_user = event_time

    if pending:
        over_events = policy.max_events is not None and len(pending) > policy.max_events
        over_characters = policy.max_characters is not None and characters > policy.max_characters
        if over_events or over_characters:
            warnings.append("oversized_turn_retained")
        if (last_user is not None and not response_in_progress
                and max(last_user + idle, pending[-1][3]) <= clock):
            close("idle", max(last_user + idle, pending[-1][3]))
    return ChunkPlan(sealed=tuple(chunks), pending_event_ids=tuple(row[0] for row in pending),
                     warnings=tuple(warnings))
