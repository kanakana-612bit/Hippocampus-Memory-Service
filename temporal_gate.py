from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from temporal_memory import parse_timestamp, timezone_info


TEMPORAL_GATE_FORMAT = "hippocampus.temporal-gate.v1"
DATE_PATTERN = re.compile(
    r"(?:(?P<year>20\d{2})\s*(?:年|[-/])\s*)?"
    r"(?P<month>\d{1,2})\s*(?:月|[-/])\s*(?P<day>\d{1,2})\s*日?"
)
PAST_WORDS = re.compile(
    r"(?:した|しました|だった|でした|終わった|行った|買った|食べた|過ぎている|過ぎています)"
    r"(?!ら|なら|場合|とき|時|際|として)|済み(?!次第|なら|の場合)|"
    r"\b(?:yesterday|ago|did|was|were|completed|went|bought|ate)\b",
    re.I,
)
FUTURE_WORDS = re.compile(
    r"予定|リマインド|思い出させ|通知|これから|明日|来週|来月|"
    r"行く|行かれる|行かれます|行きます|します|するつもり|しよう|"
    r"will|plan|remind|notify|tomorrow|next\s",
    re.I,
)
NEGATED_FUTURE_WORDS = re.compile(
    r"(?:予定|リマインド|思い出させ|通知|お知らせ|登録|設定|予約|実行)"
    r".{0,20}(?:できない|できません|しない|しません|ではない|ではありません|"
    r"はない|はありません|取り消|取消|キャンセル|無効|期限切れ)|"
    r"(?:cannot|can't|will\s+not|won't|do\s+not|don't|unable\s+to|cancel(?:led)?|expired)"
    r".{0,24}(?:plan|remind|notify|schedule|register|set|book|execute)|"
    r"(?:plan|remind|notify|schedule|register|set|book|execute)"
    r".{0,24}(?:cannot|can't|will\s+not|won't|do\s+not|don't|unable|cancel(?:led)?|expired)",
    re.I,
)
CURRENT_DATE_WORDS = re.compile(
    r"(?:今日|本日)(?:は|の日付は|の日付が)?|current date|today is",
    re.I,
)
FUTURE_COMMITMENT_WORDS = re.compile(
    r"(?:お知らせ|通知|リマインド|登録|設定|予約)(?:を)?(?:いた)?します|"
    r"思い出させ(?:ていただき)?ます|教えます|お声(?:を)?かけます|"
    r"(?:will|I'll|I will)\s+(?:remind|notify|schedule|register|set|tell)",
    re.I,
)
PROSPECTIVE_REQUEST_WORDS = re.compile(
    r"予定|リマインド|通知|お知らせ|アラート|予約|登録|設定|"
    r"教えて|思い出させて|行く|するつもり|"
    r"plan|remind|notify|schedule|alert|tell me|tomorrow|next\s",
    re.I,
)
PAST_AWARENESS_WORDS = re.compile(
    r"過ぎて(?:いる|います|おり|しま)|過去(?:です|でした|の)|"
    r"既に過ぎ|すでに過ぎ|期限切れ|already\s+passed|in\s+the\s+past|expired",
    re.I,
)
CLARIFICATION_WORDS = re.compile(
    r"どちら|確認(?:させて|して|が必要)|正しい日付|日付を(?:指定|教えて)|"
    r"指定し直|矛盾|which\s+date|clarif|correct\s+date|specify\s+the\s+date",
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


def request_temporal_issues(text: str | None, reference: datetime) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    today = reference.date()
    for sentence in sentence_spans(text or ""):
        dates = [
            (match, parsed_date(match, reference))
            for match in DATE_PATTERN.finditer(sentence)
        ]
        valid_dates = [(match, target) for match, target in dates if target is not None]
        if not valid_dates:
            continue
        prospective = bool(PROSPECTIVE_REQUEST_WORDS.search(sentence))
        for match, target in valid_dates:
            if prospective and target < today:
                issues.append(
                    {
                        "reason": "request_future_action_date_is_past",
                        "expression": match.group(0),
                        "resolved_date": target.isoformat(),
                    }
                )
            expected: date | None = None
            relative_expression: str | None = None
            if len(valid_dates) == 1 and re.search(r"明日|tomorrow", sentence, re.I):
                expected = today + timedelta(days=1)
                relative_expression = "明日"
            elif len(valid_dates) == 1 and re.search(r"今日|本日|today", sentence, re.I):
                expected = today
                relative_expression = "今日"
            if expected is not None and target != expected:
                issues.append(
                    {
                        "reason": "request_relative_absolute_date_mismatch",
                        "expression": f"{relative_expression} / {match.group(0)}",
                        "resolved_date": target.isoformat(),
                        "expected_date": expected.isoformat(),
                    }
                )
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (
            str(issue.get("reason") or ""),
            str(issue.get("expression") or ""),
            str(issue.get("resolved_date") or ""),
        )
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def candidate_resolves_request_issue(
    content: str,
    resolution_signals: list[dict[str, Any]] | None = None,
) -> bool:
    sentences = sentence_spans(content)
    has_affirmative_commitment = any(
        FUTURE_COMMITMENT_WORDS.search(sentence)
        and not NEGATED_FUTURE_WORDS.search(sentence)
        for sentence in sentences
    )
    acknowledges_problem = bool(
        NEGATED_FUTURE_WORDS.search(content)
        or PAST_AWARENESS_WORDS.search(content)
        or CLARIFICATION_WORDS.search(content)
    )
    if resolution_signals:
        kinds = {
            str(signal.get("kind") or "")
            for signal in resolution_signals
            if signal.get("source") == "response"
        }
        acknowledges_problem = acknowledges_problem or bool(
            kinds
            & {
                "past_awareness",
                "conflict_acknowledgement",
                "clarification",
                "future_negation",
            }
        )
        has_affirmative_commitment = (
            has_affirmative_commitment or "future_commitment" in kinds
        )
    return acknowledges_problem and not has_affirmative_commitment


def safe_request_clarification(
    request_content: str | None,
    reference: datetime,
) -> str | None:
    issues = request_temporal_issues(request_content, reference)
    if not issues:
        return None
    mismatch = next(
        (
            issue
            for issue in issues
            if issue.get("reason") == "request_relative_absolute_date_mismatch"
        ),
        None,
    )
    past_issue = next(
        (
            issue
            for issue in issues
            if issue.get("reason") == "request_future_action_date_is_past"
        ),
        None,
    )
    request_text = request_content or ""
    japanese = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", request_text))

    if mismatch:
        expression = str(mismatch.get("expression") or "")
        relative, _, absolute = expression.partition(" / ")
        expected = str(mismatch.get("expected_date") or "")
        resolved = str(mismatch.get("resolved_date") or "")
        if japanese:
            parts = [f"「{relative or '相対日付'}」に当たる日付は{expected}です。"]
            if absolute and resolved:
                parts.append(f"指定された「{absolute}」は{resolved}です。")
            if past_issue:
                parts.append("その指定日はすでに過ぎています。")
            parts.append("どちらの日付を意図しているか確認させてください。")
            return "".join(parts)
        parts = [f"The date corresponding to {relative or 'the relative date'} is {expected}."]
        if absolute and resolved:
            parts.append(f" The supplied date {absolute} resolves to {resolved}.")
        if past_issue:
            parts.append(" That supplied date has already passed.")
        parts.append(" Please clarify which date you intended.")
        return "".join(parts)

    issue = past_issue or issues[0]
    expression = str(issue.get("expression") or "the supplied date")
    resolved = str(issue.get("resolved_date") or "")
    if japanese:
        return (
            f"指定された「{expression}」は{resolved}で、すでに過ぎています。"
            "別の日付を意図しているか確認させてください。"
        )
    return (
        f"The supplied date {expression} resolves to {resolved} and has already passed. "
        "Please clarify whether you intended a different date."
    )


class TemporalGate:
    def validate_candidate(
        self,
        *,
        content: str,
        request_content: str | None = None,
        as_of: str | None = None,
        timezone_name: str = "UTC",
        resolution_signals: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        reference = local_reference(as_of, timezone_name)
        today = reference.date()
        claims: list[dict[str, Any]] = []
        sentences = sentence_spans(content)
        for sentence_index, sentence in enumerate(sentences):
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
                is_negated_future = bool(NEGATED_FUTURE_WORDS.search(sentence))
                is_future_statement = (
                    bool(FUTURE_WORDS.search(sentence))
                    and not is_past_statement
                    and not is_negated_future
                )
                if (
                    not is_current
                    and not is_past_statement
                    and not is_negated_future
                    and not is_future_statement
                    and sentence_index + 1 < len(sentences)
                ):
                    next_sentence = sentences[sentence_index + 1]
                    is_future_statement = bool(
                        FUTURE_COMMITMENT_WORDS.search(next_sentence)
                        and not NEGATED_FUTURE_WORDS.search(next_sentence)
                    )
                if is_current:
                    status = "verified" if target == today else "contradicted"
                    reason = "matches_current_date" if status == "verified" else "current_date_mismatch"
                    relation = "current"
                elif is_negated_future:
                    status = "verified"
                    reason = "future_action_negated"
                    relation = "negated_future"
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
        request_issues = request_temporal_issues(request_content, reference)
        rule_resolved = candidate_resolves_request_issue(content)
        if request_issues:
            resolved = candidate_resolves_request_issue(content, resolution_signals)
            for issue in request_issues:
                claims.append(
                    {
                        "sentence": None,
                        "expression": issue.get("expression"),
                        "resolved_date": issue.get("resolved_date"),
                        "expected_date": issue.get("expected_date"),
                        "claimed_relation": "request_constraint",
                        "scope": "request_response",
                        "status": "verified" if resolved else "contradicted",
                        "reason": (
                            "request_temporal_conflict_acknowledged"
                            if resolved
                            else "response_does_not_resolve_request_temporal_conflict"
                        ),
                        "request_issue": issue.get("reason"),
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
            "request_issue_count": len(request_issues),
            "request_issues": request_issues,
            "request_resolution": {
                "resolved": bool(request_issues and resolved),
                "detection": (
                    "deterministic_rules"
                    if request_issues and rule_resolved
                    else "slm_structure_v1"
                    if request_issues and resolved
                    else "none"
                ),
                "signals": resolution_signals or [],
            },
            "safe_request_clarification": safe_request_clarification(
                request_content,
                reference,
            ),
            "as_of": reference.isoformat(timespec="seconds"),
            "timezone": timezone_name,
            "policy_scope": (
                "Explicit date/current-time consistency, adjacent-sentence temporal references, "
                "and conflicts between the current request and candidate response are checked. "
                "General factual truth is not evaluated."
            ),
        }
