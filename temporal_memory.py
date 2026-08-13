from __future__ import annotations

import math
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "UTC"
TEMPORAL_SCOPES = ("current", "historical", "future", "all")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def timezone_info(name: str | None) -> ZoneInfo:
    candidate = str(name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {candidate}") from exc


def timezone_name(name: str | None) -> str:
    zone = timezone_info(name)
    return str(zone.key)


def parse_timestamp(
    value: Any,
    default_timezone: str = DEFAULT_TIMEZONE,
    *,
    field_name: str = "timestamp",
    fallback: str | None = None,
) -> datetime:
    def from_epoch(number: float) -> datetime:
        seconds = number / 1000.0 if abs(number) >= 100_000_000_000 else number
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    if value is None or value == "":
        if fallback is None:
            raise ValueError(f"{field_name} is required.")
        value = fallback

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = from_epoch(float(value))
    else:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{field_name} is required.")
        if text.replace(".", "", 1).isdigit():
            parsed = from_epoch(float(text))
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"Invalid {field_name}: {value}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_info(default_timezone))
    return parsed.astimezone(timezone.utc)


def normalize_timestamp(
    value: Any,
    default_timezone: str = DEFAULT_TIMEZONE,
    *,
    field_name: str = "timestamp",
    fallback: str | None = None,
) -> str:
    return parse_timestamp(
        value,
        default_timezone,
        field_name=field_name,
        fallback=fallback,
    ).isoformat(timespec="milliseconds")


def optional_timestamp(
    value: Any,
    default_timezone: str = DEFAULT_TIMEZONE,
    *,
    field_name: str = "timestamp",
) -> str | None:
    if value is None or value == "":
        return None
    return normalize_timestamp(value, default_timezone, field_name=field_name)


def validate_window(valid_from: str | None, valid_until: str | None) -> None:
    if valid_from and valid_until:
        start = parse_timestamp(valid_from, field_name="valid_from")
        end = parse_timestamp(valid_until, field_name="valid_until")
        if end <= start:
            raise ValueError("valid_until must be later than valid_from.")


def seconds_between(later: str | None, earlier: str | None) -> float | None:
    if not later or not earlier:
        return None
    return (parse_timestamp(later) - parse_timestamp(earlier)).total_seconds()


def local_timestamp(value: str, target_timezone: str) -> str:
    zone = timezone_info(target_timezone)
    return parse_timestamp(value).astimezone(zone).isoformat(timespec="milliseconds")


def temporal_state(record: dict[str, Any], as_of: str | None = None) -> str:
    reference = parse_timestamp(as_of, fallback=utc_now(), field_name="as_of")
    valid_from = record.get("valid_from")
    if valid_from and parse_timestamp(valid_from) > reference:
        return "scheduled"
    valid_until = record.get("valid_until")
    if valid_until and parse_timestamp(valid_until) <= reference:
        return "superseded" if record.get("superseded_by") else "expired"
    if record.get("superseded_by") and not valid_until:
        return "superseded"
    expires_at = record.get("expires_at")
    if expires_at and parse_timestamp(expires_at) <= reference:
        return "expired"
    return "current"


def temporal_relevance(record: dict[str, Any], as_of: str | None = None) -> float:
    state = temporal_state(record, as_of)
    if state == "superseded":
        return 0.10
    if state == "expired":
        return 0.25
    if state == "scheduled":
        return 0.55

    event_time = record.get("event_time") or record.get("created_at")
    if not event_time:
        return 0.70
    reference = parse_timestamp(as_of, fallback=utc_now(), field_name="as_of")
    event = parse_timestamp(event_time)
    age_days = max(0.0, (reference - event).total_seconds() / 86400.0)
    half_life_days = 365.0 if record.get("memory_type") == "episodic" else 1460.0
    return max(0.20, math.pow(0.5, age_days / half_life_days))


def duration_text(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    future = seconds < 0
    value = abs(seconds)
    if value < 60:
        text = f"{int(value)} seconds"
    elif value < 3600:
        text = f"{int(value // 60)} minutes"
    elif value < 86400:
        text = f"{value / 3600:.1f} hours"
    else:
        text = f"{value / 86400:.1f} days"
    return f"in {text}" if future else text


def infer_temporal_window(
    content: str,
    event_time: str,
    source_timezone: str,
) -> dict[str, Any]:
    text_value = str(content or "")
    zone = timezone_info(source_timezone)
    local_event = parse_timestamp(event_time).astimezone(zone)

    def day_window(day_value: datetime, expression: str, precision: str = "day") -> dict[str, Any]:
        start_local = datetime.combine(day_value.date(), time.min, tzinfo=zone)
        end_local = start_local + timedelta(days=1)
        return {
            "valid_from": start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "valid_until": end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "temporal_expression": expression,
            "temporal_precision": precision,
        }

    explicit = re.search(
        r"(?P<year>20\d{2})\s*(?:年|[-/])\s*(?P<month>\d{1,2})\s*(?:月|[-/])\s*(?P<day>\d{1,2})\s*日?",
        text_value,
    )
    if explicit:
        try:
            target = datetime(
                int(explicit.group("year")),
                int(explicit.group("month")),
                int(explicit.group("day")),
                tzinfo=zone,
            )
        except ValueError:
            return {}
        window = day_window(target, explicit.group(0))
        if re.search(r"(?:まで|期限|締切|by\b)", text_value, flags=re.IGNORECASE):
            window["valid_from"] = None
        elif re.search(r"(?:から|開始|from\b)", text_value, flags=re.IGNORECASE):
            window["valid_until"] = None
        return window

    lowered = text_value.casefold()
    if "明日" in text_value or "tomorrow" in lowered:
        return day_window(local_event + timedelta(days=1), "明日" if "明日" in text_value else "tomorrow")
    if "今日" in text_value or re.search(r"\btoday\b", lowered):
        return day_window(local_event, "今日" if "今日" in text_value else "today")
    if "来週" in text_value or "next week" in lowered:
        days_until_next_monday = 7 - local_event.weekday()
        start = local_event + timedelta(days=days_until_next_monday)
        start_local = datetime.combine(start.date(), time.min, tzinfo=zone)
        end_local = start_local + timedelta(days=7)
        return {
            "valid_from": start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "valid_until": end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "temporal_expression": "来週" if "来週" in text_value else "next week",
            "temporal_precision": "week",
        }
    if "来月" in text_value or "next month" in lowered:
        year = local_event.year + (1 if local_event.month == 12 else 0)
        month = 1 if local_event.month == 12 else local_event.month + 1
        next_year = year + (1 if month == 12 else 0)
        next_month = 1 if month == 12 else month + 1
        start_local = datetime(year, month, 1, tzinfo=zone)
        end_local = datetime(next_year, next_month, 1, tzinfo=zone)
        return {
            "valid_from": start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "valid_until": end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "temporal_expression": "来月" if "来月" in text_value else "next month",
            "temporal_precision": "month",
        }
    return {}
