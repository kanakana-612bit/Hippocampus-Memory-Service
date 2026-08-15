from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any

from temporal_memory import parse_timestamp, timezone_info


TEMPORAL_GATE_FORMAT = "hippocampus.temporal-gate.v1"
DATE_PATTERN = re.compile(
    r"(?:(?P<year>20\d{2})\s*(?:年|[-/])\s*)?"
    r"(?P<month>\d{1,2})\s*(?:月|[-/])\s*(?P<day>\d{1,2})\s*日?"
)
PAST_WORDS = re.compile(
    r"した|しました|だった|でした|済み|終わった|行った|買った|食べた|"
    r"yesterday|ago|did|was|were|completed|went|bought|ate",
    re.I,
)
FUTURE_WORDS = re.compile(
    r"予定|リマインド|思い出させ|通知|これから|明日|来週|来月|"
    r"行きます|します|するつもり|しよう|will|plan|remind|notify|tomorrow|next\s",
    re.I,
)
CURRENT_DATE_WORDS = re.compile(
    r"(?:今日|本日)(?:は|の日付は|の日付が)?|current date|today is",
    re.I,
)


def sentence_spans(text: str) -> list[str]:
    return [
        match.group(0).strip()
        for match in re.finditer(r"[^。！？!?\n]+(?:[。！？!?]+|\n|$)", text or "")
        if match.group(0).strip()
    ]


def local_reference(as_of: str | None, timezone_name: str) -> datetime:
    reference = parse_timestamp(
        as_of,
        timezone_name,
        field_name="as_of",
        fallback=datetime.now(timezone.utc).isoformat(),
    )
    return reference.astimezone(timezone_info(timezone_name))


def parsed_date(match: re.Match[str], reference: datetime) -> date | None:
    year = int(match.group("year") or reference.year)
    try:
        return date(year, int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None


class TemporalGate:
    def validate_candidate(
        self,
        *,
        content: str,
        as_of: str | None = None,
        timezone_name: str = "UTC",
    ) -> dict[str, Any]:
        reference = local_reference(as_of, timezone_name)
        today = reference.date()
        claims: list[dict[str, Any]] = []
        for sentence in sentence_spans(content):
            for match in DATE_PATTERN.finditer(sentence):
                target = parsed_date(match, reference)
                if target is None:
                    claims.append(
                        {
                            "sentence": sentence,
                            "expression": match.group(0),
                            "status": "unverified",
                            "reason": "invalid_calendar_date",
                        }
                    )
                    continue
                is_current = bool(CURRENT_DATE_WORDS.search(sentence))
                is_past_statement = bool(PAST_WORDS.search(sentence))
                is_future_statement = bool(FUTURE_WORDS.search(sentence)) and not is_past_statement
                if is_current:
                    status = "verified" if target == today else "contradicted"
                    reason = "matches_current_date" if status == "verified" else "current_date_mismatch"
                    relation = "current"
                elif is_future_statement:
                    status = "verified" if target >= today else "contradicted"
                    reason = "future_date_consistent" if status == "verified" else "future_action_date_is_past"
                    relation = "future"
                elif is_past_statement:
                    status = "verified" if target <= today else "contradicted"
                    reason = "past_date_consistent" if status == "verified" else "past_event_date_is_future"
                    relation = "past"
                else:
                    status = "unverified"
                    reason = "date_has_no_temporal_modality"
                    relation = "unspecified"
                claims.append(
                    {
                        "sentence": sentence,
                        "expression": match.group(0),
                        "resolved_date": target.isoformat(),
                        "claimed_relation": relation,
                        "status": status,
                        "reason": reason,
                    }
                )
        counts = {
            status: sum(1 for claim in claims if claim["status"] == status)
            for status in ("verified", "unverified", "contradicted")
        }
        decision = "reject" if counts["contradicted"] else "allow"
        return {
            "format": TEMPORAL_GATE_FORMAT,
            "candidate_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "decision": decision,
            "applicable": bool(claims),
            "claim_counts": counts,
            "claims": claims,
            "as_of": reference.isoformat(timespec="seconds"),
            "timezone": timezone_name,
            "policy_scope": (
                "Only explicit date/current-time consistency and past/future modality are checked. "
                "General factual truth is not evaluated."
            ),
        }
