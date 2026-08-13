from __future__ import annotations

import difflib
import hashlib
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from audit_ledger import AuditLedger, object_digest
from checkpoint_security import CheckpointSecurity


GATE_FORMAT = "hippocampus-attribution-gate-v1"
DIRECT_ORIGINS = {"original"}
ACTOR_ALIASES = {
    "user": "user",
    "ユーザー": "user",
    "ユーザ": "user",
    "利用者": "user",
    "依頼者": "user",
    "あなた": "user",
    "君": "user",
    "きみ": "user",
    "assistant": "assistant",
    "agent": "assistant",
    "アシスタント": "assistant",
    "エージェント": "assistant",
    "AI": "assistant",
    "私": "assistant",
    "僕": "assistant",
    "システム": "system",
    "system": "system",
}

JAPANESE_ACTORS = "ユーザー|ユーザ|利用者|依頼者|あなた|君|きみ|アシスタント|エージェント|AI|私|僕|システム"
JAPANESE_VERBS = (
    r"言(?:った|っていた|ってた|いました)|述べ(?:た|ていた|ました)|"
    r"話し(?:た|ていた|てた|ました)|語(?:った|っていた)|"
    r"求め(?:た|ていた)|頼(?:んだ|んでいた)|依頼し(?:た|ていた)|"
    r"指示し(?:た|ていた)|希望し(?:た|ていた)|望(?:んだ|んでいた)|"
    r"考え(?:た|ていた)|整理し(?:た|ていた)|提案し(?:た|ていた)|"
    r"主張し(?:た|ていた)|決め(?:た|ていた)|選ん(?:だ|でいた)|"
    r"好(?:きだ|んでいた)|嫌(?:いだ|っていた)"
)
ENGLISH_ACTORS = r"you|the user|user|I|we|the assistant|assistant|the agent|agent"
ENGLISH_VERBS = (
    r"said|mentioned|stated|told me|asked|requested|instructed|wanted|preferred|"
    r"believed|thought|argued|suggested|proposed|decided|chose"
)

EVENT_MARKER = re.compile(r"\[\[event:([A-Za-z0-9_.:-]+)\]\]")
MEMORY_MARKER = re.compile(r"\[\[memory:([A-Za-z0-9_.:-]+)\]\]")
ANY_MARKER = re.compile(r"\[\[(?:event|memory):[A-Za-z0-9_.:-]+\]\]")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_actor(value: str | None) -> str:
    raw = str(value or "unknown").strip()
    if raw in ACTOR_ALIASES:
        return ACTOR_ALIASES[raw]
    lowered = raw.lower()
    if lowered in {"you", "the user", "user"}:
        return "user"
    if lowered in {"i", "we", "assistant", "the assistant", "agent", "the agent"}:
        return "assistant"
    if lowered == "system":
        return "system"
    return lowered or "unknown"


def normalize_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    normalized = ANY_MARKER.sub("", normalized)
    normalized = re.sub(
        rf"(?:{JAPANESE_ACTORS})(?:が|は|の)?(?:以前|前に|かつて|過去に)?",
        "",
        normalized,
    )
    normalized = re.sub(rf"(?:{JAPANESE_VERBS})", "", normalized)
    normalized = re.sub(rf"\b(?:{ENGLISH_ACTORS})\b", "", normalized, flags=re.I)
    normalized = re.sub(rf"\b(?:{ENGLISH_VERBS})\b", "", normalized, flags=re.I)
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", normalized)


def _ngrams(value: str, size: int = 2) -> set[str]:
    if len(value) <= size:
        return {value} if value else set()
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def text_similarity(statement: str, evidence: str) -> float:
    left = normalize_for_match(statement)
    right = normalize_for_match(evidence)
    if not left or not right:
        return 0.0
    if left in right or right in left:
        ratio = min(len(left), len(right)) / max(len(left), len(right))
        return max(0.76, min(1.0, 0.76 + 0.24 * ratio))
    left_words = set(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", statement).lower()))
    right_words = set(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", evidence).lower()))
    stop_words = {
        "a", "an", "the", "that", "this", "it", "is", "are", "was", "were",
        "be", "been", "as", "to", "of", "and", "or", "also", "can", "could",
        "previously", "before", "you", "i", "we", "user", "assistant",
    }
    left_terms = left_words - stop_words
    right_terms = right_words - stop_words
    ascii_heavy = len(re.sub(r"[^a-z0-9]", "", statement.lower())) >= max(3, len(left) // 2)
    if ascii_heavy and left_terms and right_terms:
        overlap = len(left_terms & right_terms) / len(left_terms | right_terms)
        sequence = difflib.SequenceMatcher(None, left, right).ratio()
        return round(0.72 * overlap + 0.28 * sequence, 6)
    left_grams = _ngrams(left)
    right_grams = _ngrams(right)
    dice = (
        2.0 * len(left_grams & right_grams) / (len(left_grams) + len(right_grams))
        if left_grams and right_grams
        else 0.0
    )
    sequence = difflib.SequenceMatcher(None, left, right).ratio()
    return round(max(dice, sequence), 6)


def claim_kind(verb: str) -> str:
    normalized = verb.lower()
    if re.search(r"求め|頼|依頼|指示|希望|望|asked|requested|instructed|wanted", normalized):
        return "request"
    if re.search(r"好|嫌|preferred|chose|選|決め|decided", normalized):
        return "preference"
    if re.search(r"考|主張|believed|thought|argued", normalized):
        return "belief"
    if re.search(r"提案|suggested|proposed", normalized):
        return "proposal"
    return "speech"


def sentence_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^。！？!?\n]+(?:[。！？!?]+|\n|$)", text or ""):
        sentence = match.group(0).strip()
        if sentence:
            spans.append((match.start(), match.end(), sentence))
    return spans


def extract_statement(sentence: str, actor: str, verb: str) -> str:
    without_markers = ANY_MARKER.sub("", sentence)
    quoted = re.search(r"「([^」]{2,})」|『([^』]{2,})』|[\"“]([^\"”]{2,})[\"”]", without_markers)
    if quoted:
        return next(group for group in quoted.groups() if group).strip()
    actor_index = without_markers.lower().find(actor.lower())
    verb_index = without_markers.lower().rfind(verb.lower())
    if re.fullmatch(ENGLISH_ACTORS, actor, flags=re.I) and verb_index >= 0:
        after = without_markers[verb_index + len(verb) :]
        after = re.sub(r"^(?:\s+that|\s+to|\s+me\s+to)\s+", "", after, flags=re.I)
        after = after.strip(" .,!?:;")
        if len(after) >= 2:
            return after
    if actor_index >= 0 and verb_index > actor_index:
        between = without_markers[actor_index + len(actor) : verb_index]
        between = re.sub(r"^(?:が|は|の)?(?:以前|前に|かつて|過去に)?", "", between).strip(" 、,:：")
        if len(between) >= 2:
            return between
    return without_markers.strip()


def extract_claims(text: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for start, end, sentence in sentence_spans(text):
        found: list[tuple[str, str]] = []
        for match in re.finditer(
            rf"(?P<actor>{JAPANESE_ACTORS})(?:が|は|の)?(?:以前|前に|かつて|過去に)?[^。！？!?\n]{{0,180}}?(?P<verb>{JAPANESE_VERBS})",
            sentence,
            flags=re.I,
        ):
            found.append((match.group("actor"), match.group("verb")))
        for match in re.finditer(
            rf"\b(?P<actor>{ENGLISH_ACTORS})\b[^.!?\n]{{0,180}}?\b(?P<verb>{ENGLISH_VERBS})\b",
            sentence,
            flags=re.I,
        ):
            found.append((match.group("actor"), match.group("verb")))
        for actor, verb in found:
            event_ids = EVENT_MARKER.findall(sentence)
            memory_ids = MEMORY_MARKER.findall(sentence)
            claims.append(
                {
                    "claim_id": f"claim_{uuid.uuid4().hex}",
                    "sentence": sentence,
                    "start": start,
                    "end": end,
                    "claimed_actor_role": canonical_actor(actor),
                    "claim_kind": claim_kind(verb),
                    "statement": extract_statement(sentence, actor, verb),
                    "event_ids": list(dict.fromkeys(event_ids)),
                    "memory_ids": list(dict.fromkeys(memory_ids)),
                    "detection": "deterministic_phrase_v1",
                }
            )
    return claims


@dataclass
class Evidence:
    reference_type: str
    reference_id: str
    actor_role: str
    actor_id: str | None
    content_origin: str
    source_channel: str
    content: str
    object_valid: bool
    audit_sequence: int | None
    integrity: str
    acquisition_mode: str | None = None
    epistemic_status: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.reference_type}:{self.reference_id}"

    def public(self, similarity: float | None = None) -> dict[str, Any]:
        result = {
            "reference_type": self.reference_type,
            "reference_id": self.reference_id,
            "actor_role": self.actor_role,
            "content_origin": self.content_origin,
            "source_channel": self.source_channel,
            "object_valid": self.object_valid,
            "integrity": self.integrity,
            "acquisition_mode": self.acquisition_mode,
            "epistemic_status": self.epistemic_status,
        }
        if similarity is not None:
            result["similarity"] = similarity
        return result


class AttributionGate:
    def __init__(
        self,
        security: CheckpointSecurity,
        audit: AuditLedger | None = None,
    ) -> None:
        self.security = security
        self.audit = audit or AuditLedger()

    def _integrity_context(self, con: sqlite3.Connection) -> dict[str, Any]:
        audit = self.audit.verify(con, verify_objects=False)
        checkpoints = self.security.verify_checkpoints(con)
        latest = self.security.latest_checkpoint(con)
        return {
            "audit_valid": bool(audit["chain_valid"]),
            "checkpoint_valid": bool(checkpoints["valid"]),
            "checkpoint_sequence": int(latest["sequence_end"]) if latest else 0,
        }

    def _raw_evidence(
        self,
        con: sqlite3.Connection,
        event_ids: list[str],
        conversation_id: str | None,
        integrity: dict[str, Any],
    ) -> list[Evidence]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_ids:
            clauses.append(f"id IN ({','.join('?' for _ in event_ids)})")
            params.extend(event_ids)
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if not clauses:
            return []
        rows = con.execute(
            f"SELECT * FROM raw_messages WHERE {' OR '.join(clauses)} ORDER BY event_sequence DESC, created_at DESC LIMIT 200",
            params,
        ).fetchall()
        evidence: list[Evidence] = []
        for row in rows:
            audit_row = None
            if row["latest_audit_event_id"]:
                audit_row = con.execute(
                    "SELECT sequence, actor_role, content_origin FROM audit_events WHERE event_id=?",
                    (row["latest_audit_event_id"],),
                ).fetchone()
            current_digest = object_digest(row)
            object_valid = bool(
                integrity["audit_valid"]
                and audit_row
                and row["latest_object_digest"] == current_digest
                and canonical_actor(audit_row["actor_role"])
                == canonical_actor(row["actor_role"] or row["role"])
                and str(audit_row["content_origin"]) == str(row["content_origin"])
            )
            sequence = int(audit_row["sequence"]) if audit_row else None
            level = "invalid"
            if object_valid:
                level = (
                    "checkpointed"
                    if integrity["checkpoint_valid"]
                    and sequence is not None
                    and sequence <= integrity["checkpoint_sequence"]
                    else "verified_local"
                )
            evidence.append(
                Evidence(
                    reference_type="event",
                    reference_id=str(row["id"]),
                    actor_role=canonical_actor(row["actor_role"] or row["role"]),
                    actor_id=row["actor_id"],
                    content_origin=str(row["content_origin"] or "unknown"),
                    source_channel=str(row["source_channel"] or "unknown"),
                    content=str(row["content"] or ""),
                    object_valid=object_valid,
                    audit_sequence=sequence,
                    integrity=level,
                )
            )
        return evidence

    def _memory_evidence(
        self,
        con: sqlite3.Connection,
        memory_ids: list[str],
        integrity: dict[str, Any],
    ) -> list[Evidence]:
        if not memory_ids:
            return []
        rows = con.execute(
            f"SELECT * FROM memories WHERE id IN ({','.join('?' for _ in memory_ids)}) LIMIT 100",
            memory_ids,
        ).fetchall()
        evidence: list[Evidence] = []
        for row in rows:
            audit_row = None
            if row["latest_audit_event_id"]:
                audit_row = con.execute(
                    "SELECT sequence, actor_role, content_origin FROM audit_events WHERE event_id=?",
                    (row["latest_audit_event_id"],),
                ).fetchone()
            current_digest = object_digest(row)
            object_valid = bool(
                integrity["audit_valid"]
                and audit_row
                and row["latest_object_digest"] == current_digest
                and canonical_actor(audit_row["actor_role"])
                == canonical_actor(row["actor_role"])
            )
            sequence = int(audit_row["sequence"]) if audit_row else None
            level = "invalid"
            if object_valid:
                level = (
                    "checkpointed"
                    if integrity["checkpoint_valid"]
                    and sequence is not None
                    and sequence <= integrity["checkpoint_sequence"]
                    else "verified_local"
                )
            evidence.append(
                Evidence(
                    reference_type="memory",
                    reference_id=str(row["id"]),
                    actor_role=canonical_actor(row["actor_role"]),
                    actor_id=row["actor_id"],
                    content_origin=str(row["content_origin"] or "unknown"),
                    source_channel=str(row["source_channel"] or "unknown"),
                    content=str(row["content"] or ""),
                    object_valid=object_valid,
                    audit_sequence=sequence,
                    integrity=level,
                    acquisition_mode=str(row["acquisition_mode"]),
                    epistemic_status=str(row["epistemic_status"]),
                )
            )
        return evidence

    def resolve_evidence(
        self,
        con: sqlite3.Connection,
        *,
        event_ids: list[str] | None = None,
        memory_ids: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> tuple[list[Evidence], dict[str, Any]]:
        event_ids = list(dict.fromkeys(event_ids or []))[:200]
        memory_ids = list(dict.fromkeys(memory_ids or []))[:100]
        integrity = self._integrity_context(con)
        evidence = self._raw_evidence(con, event_ids, conversation_id, integrity)
        evidence.extend(self._memory_evidence(con, memory_ids, integrity))
        deduped = {item.ref: item for item in evidence}
        return list(deduped.values()), integrity

    @staticmethod
    def _memory_can_support(claim: dict[str, Any], evidence: Evidence) -> bool:
        return bool(
            evidence.reference_type == "memory"
            and evidence.actor_role == "user"
            and evidence.acquisition_mode == "user_explicit"
            and evidence.epistemic_status == "confirmed"
            and claim["claim_kind"] in {"request", "preference", "belief", "proposal"}
        )

    def _evidence_can_support(self, claim: dict[str, Any], evidence: Evidence) -> bool:
        return bool(
            evidence.object_valid
            and (
                (
                    evidence.reference_type == "event"
                    and evidence.content_origin in DIRECT_ORIGINS
                )
                or self._memory_can_support(claim, evidence)
            )
        )

    def evaluate_claim(
        self,
        claim: dict[str, Any],
        evidence: list[Evidence],
        threshold: float,
    ) -> dict[str, Any]:
        explicit_refs = {
            *(f"event:{item}" for item in claim.get("event_ids") or []),
            *(f"memory:{item}" for item in claim.get("memory_ids") or []),
        }
        considered = [item for item in evidence if not explicit_refs or item.ref in explicit_refs]
        scored = sorted(
            ((text_similarity(claim["statement"], item.content), item) for item in considered),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best = scored[0] if scored else (0.0, None)
        same_actor = [
            (score, item)
            for score, item in scored
            if item.actor_role == claim["claimed_actor_role"]
            and self._evidence_can_support(claim, item)
        ]
        same_score, same = same_actor[0] if same_actor else (0.0, None)

        status = "unverified"
        reason = "no_matching_evidence"
        if explicit_refs and not considered:
            reason = "cited_evidence_not_allowed_or_missing"
        elif best and not best.object_valid:
            reason = "evidence_integrity_invalid"
        elif explicit_refs and best and not self._evidence_can_support(claim, best):
            status = "contradicted"
            reason = "derived_evidence_cannot_support_direct_attribution"
        elif explicit_refs and best and best.actor_role != claim["claimed_actor_role"]:
            status = "contradicted"
            reason = "cited_actor_mismatch"
        elif same and same_score >= threshold and (
            best is None
            or best.actor_role == claim["claimed_actor_role"]
            or same_score >= best_score - 0.08
        ):
            status = "verified"
            reason = "actor_and_proposition_match"
        elif best and best_score >= threshold and best.actor_role != claim["claimed_actor_role"]:
            status = "contradicted"
            reason = "best_evidence_actor_mismatch"
        elif best and best_score >= threshold and not self._evidence_can_support(claim, best):
            status = "contradicted"
            reason = "best_evidence_is_derived"
        elif same and same_score >= max(0.28, threshold - 0.12):
            reason = "possible_actor_match_but_statement_match_is_weak"

        result = {
            **claim,
            "status": status,
            "reason": reason,
            "best_evidence": best.public(best_score) if best else None,
            "matching_evidence": same.public(same_score) if same else None,
        }
        return result

    def validate_candidate(
        self,
        con: sqlite3.Connection,
        *,
        content: str,
        conversation_id: str | None = None,
        event_ids: list[str] | None = None,
        memory_ids: list[str] | None = None,
        claims: list[dict[str, Any]] | None = None,
        threshold: float = 0.46,
    ) -> dict[str, Any]:
        extracted = claims if claims is not None else extract_claims(content)
        normalized_claims: list[dict[str, Any]] = []
        cited_events = EVENT_MARKER.findall(content)
        cited_memories = MEMORY_MARKER.findall(content)
        for item in extracted:
            normalized_claims.append(
                {
                    "claim_id": str(item.get("claim_id") or f"claim_{uuid.uuid4().hex}"),
                    "sentence": str(item.get("sentence") or item.get("statement") or ""),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "claimed_actor_role": canonical_actor(item.get("claimed_actor_role")),
                    "claim_kind": str(item.get("claim_kind") or "speech"),
                    "statement": str(item.get("statement") or item.get("sentence") or ""),
                    "event_ids": list(dict.fromkeys(item.get("event_ids") or cited_events)),
                    "memory_ids": list(dict.fromkeys(item.get("memory_ids") or cited_memories)),
                    "detection": str(item.get("detection") or "client_structured"),
                }
            )
        all_event_ids = list(
            dict.fromkeys(
                [*(event_ids or []), *cited_events]
                + [value for claim in normalized_claims for value in claim["event_ids"]]
            )
        )
        all_memory_ids = list(
            dict.fromkeys(
                [*(memory_ids or []), *cited_memories]
                + [value for claim in normalized_claims for value in claim["memory_ids"]]
            )
        )
        evidence, integrity = self.resolve_evidence(
            con,
            event_ids=all_event_ids,
            memory_ids=all_memory_ids,
            conversation_id=conversation_id,
        )
        evaluated = [
            self.evaluate_claim(claim, evidence, threshold) for claim in normalized_claims
        ]
        counts = {
            status: sum(1 for item in evaluated if item["status"] == status)
            for status in ("verified", "unverified", "contradicted")
        }
        if counts["contradicted"]:
            decision = "reject"
        elif counts["unverified"]:
            decision = "unverified"
        else:
            decision = "allow"
        return {
            "format": GATE_FORMAT,
            "candidate_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "decision": decision,
            "applicable": bool(evaluated),
            "safe_content": ANY_MARKER.sub("", content),
            "claim_counts": counts,
            "claims": evaluated,
            "evidence_scope": {
                "event_ids": all_event_ids,
                "memory_ids": all_memory_ids,
                "conversation_scoped": bool(conversation_id),
                "resolved_count": len(evidence),
                "audit_chain_valid": integrity["audit_valid"],
                "checkpoint_valid": integrity["checkpoint_valid"],
            },
            "policy_scope": (
                "Only attribution of past speech, requests, preferences, beliefs, and proposals is checked. "
                "The truth or quality of the answer is not evaluated."
            ),
        }

    def select_candidates(
        self,
        con: sqlite3.Connection,
        *,
        candidates: list[dict[str, Any]],
        conversation_id: str | None = None,
        event_ids: list[str] | None = None,
        memory_ids: list[str] | None = None,
        threshold: float = 0.46,
    ) -> dict[str, Any]:
        evaluated: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates[:12]):
            content = str(candidate.get("content") or "")
            validation = self.validate_candidate(
                con,
                content=content,
                conversation_id=conversation_id,
                event_ids=event_ids,
                memory_ids=memory_ids,
                claims=candidate.get("claims"),
                threshold=threshold,
            )
            quality = float(candidate.get("quality_score") or 0.0)
            rank = (
                2 if validation["decision"] == "allow" else 1 if validation["decision"] == "unverified" else 0,
                validation["claim_counts"]["verified"],
                -validation["claim_counts"]["unverified"],
                quality,
                -index,
            )
            evaluated.append(
                {
                    "candidate_id": str(candidate.get("candidate_id") or f"candidate_{index + 1}"),
                    "quality_score": quality,
                    "validation": validation,
                    "rank": list(rank),
                }
            )
        allowed = [item for item in evaluated if item["validation"]["decision"] == "allow"]
        selected = max(allowed, key=lambda item: tuple(item["rank"])) if allowed else None
        regeneration_feedback: list[dict[str, Any]] = []
        if selected is None:
            for item in evaluated:
                for claim in item["validation"]["claims"]:
                    if claim["status"] != "verified":
                        regeneration_feedback.append(
                            {
                                "candidate_id": item["candidate_id"],
                                "claimed_actor_role": claim["claimed_actor_role"],
                                "status": claim["status"],
                                "reason": claim["reason"],
                                "evidence_actor_role": (
                                    claim["best_evidence"] or {}
                                ).get("actor_role"),
                                "evidence_reference": (
                                    f"{claim['best_evidence']['reference_type']}:"
                                    f"{claim['best_evidence']['reference_id']}"
                                    if claim.get("best_evidence")
                                    else None
                                ),
                            }
                        )
        return {
            "format": GATE_FORMAT,
            "decision": "selected" if selected else "regenerate",
            "selected_candidate_id": selected["candidate_id"] if selected else None,
            "selected_content": selected["validation"]["safe_content"] if selected else None,
            "regeneration_required": selected is None,
            "regeneration_feedback": regeneration_feedback[:12],
            "fallback_content": (
                "過去の発言者を根拠から確認できなかったため、帰属を断定する応答は表示しませんでした。"
                if selected is None
                else None
            ),
            "candidates": evaluated,
        }
