from __future__ import annotations

import hashlib
import re
from typing import Any


DEFAULT_CAPTURE_THRESHOLD = 0.50

POSITIVE_PATTERNS = (
    "嬉しい",
    "うれしい",
    "感動",
    "最高",
    "素晴らしい",
    "楽しい",
    "安心",
    "幸せ",
    "期待以上",
    "glad",
    "happy",
    "wonderful",
    "excellent",
    "amazing",
)
NEGATIVE_PATTERNS = (
    "悲しい",
    "怒り",
    "怒って",
    "不安",
    "怖い",
    "困った",
    "残念",
    "嫌い",
    "つらい",
    "苦しい",
    "失望",
    "sad",
    "angry",
    "afraid",
    "anxious",
    "disappointed",
)
SURPRISE_PATTERNS = ("驚いた", "びっくり", "まさか", "鳥肌", "surprised", "unexpected")
INTENSIFIER_PATTERNS = (
    "非常に",
    "とても",
    "本当に",
    "かなり",
    "すごく",
    "ものすごく",
    "絶対",
    "very",
    "really",
    "extremely",
)
IMPORTANCE_PATTERNS = (
    "重要",
    "大切",
    "忘れない",
    "必ず",
    "優先",
    "欠かせない",
    "important",
    "critical",
    "must",
)
UNFINISHED_PATTERNS = (
    "次に",
    "今後",
    "後で",
    "明日",
    "予定",
    "したい",
    "する必要",
    "必要がある",
    "やろう",
    "続け",
    "未完",
    "残っている",
    "検討する",
    "課題",
    "todo",
    "next",
    "later",
    "plan to",
    "need to",
)
COMPLETION_PATTERNS = ("完了", "終わった", "済み", "解決した", "done", "completed", "resolved")
PREFERENCE_PATTERNS = (
    "好き",
    "嫌い",
    "好む",
    "希望する",
    "優先する",
    "いつも",
    "今後は",
    "方針",
    "prefer",
    "always",
    "never",
)
PROCEDURAL_PATTERNS = (
    "手順",
    "方法",
    "やり方",
    "ルール",
    "するときは",
    "する場合は",
    "workflow",
    "procedure",
    "steps",
)
CORRECTION_PATTERNS = (
    "訂正",
    "違う",
    "ではなく",
    "正しくは",
    "誤り",
    "変更する",
    "修正する",
    "correction",
    "not that",
    "rather than",
)
CONFIRMATION_PATTERNS = (
    "その通り",
    "合っている",
    "正しい",
    "これでよい",
    "これでいい",
    "確認した",
    "exactly",
    "correct",
    "that's right",
)


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def normalize_content(content: str) -> str:
    return re.sub(r"\s+", " ", content or "").strip()


def content_fingerprint(content: str) -> str:
    normalized = normalize_content(content).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def excerpt(content: str, limit: int = 220) -> str:
    compact = normalize_content(content)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def pattern_hits(content: str, patterns: tuple[str, ...]) -> list[str]:
    normalized = content.casefold()
    return [pattern for pattern in patterns if pattern.casefold() in normalized]


def signal_score(hits: list[str], base: float = 0.58, increment: float = 0.12) -> float:
    if not hits:
        return 0.0
    return clamp(base + increment * (len(hits) - 1))


def term_similarity(left: list[str], right: list[str]) -> float:
    a = {item.casefold() for item in left if item}
    b = {item.casefold() for item in right if item}
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    if overlap == 0:
        return 0.0
    jaccard = overlap / len(a | b)
    containment = overlap / min(len(a), len(b))
    return clamp(max(jaccard, containment * 0.85))


def analyze_user_message(content: str, previous_assistant: str | None = None) -> dict[str, Any]:
    compact = normalize_content(content)
    if not compact:
        return {"eligible": False, "reason": "empty message"}

    positive_hits = pattern_hits(compact, POSITIVE_PATTERNS)
    negative_hits = pattern_hits(compact, NEGATIVE_PATTERNS)
    surprise_hits = pattern_hits(compact, SURPRISE_PATTERNS)
    intensifier_hits = pattern_hits(compact, INTENSIFIER_PATTERNS)
    importance_hits = pattern_hits(compact, IMPORTANCE_PATTERNS)
    unfinished_hits = pattern_hits(compact, UNFINISHED_PATTERNS)
    completion_hits = pattern_hits(compact, COMPLETION_PATTERNS)
    preference_hits = pattern_hits(compact, PREFERENCE_PATTERNS)
    procedural_hits = pattern_hits(compact, PROCEDURAL_PATTERNS)
    correction_hits = pattern_hits(compact, CORRECTION_PATTERNS)
    confirmation_hits = pattern_hits(compact, CONFIRMATION_PATTERNS)

    emotion_count = len(positive_hits) + len(negative_hits) + len(surprise_hits)
    affect_intensity = 0.0
    if emotion_count:
        affect_intensity = clamp(
            0.56
            + 0.13 * (emotion_count - 1)
            + 0.10 * len(intensifier_hits)
            + min(0.14, 0.035 * (compact.count("!") + compact.count("！"))),
        )
    if positive_hits and negative_hits:
        valence = "mixed"
    elif negative_hits:
        valence = "negative"
    elif positive_hits:
        valence = "positive"
    else:
        valence = "neutral"

    unfinished_score = signal_score(unfinished_hits, base=0.62)
    if completion_hits and unfinished_score:
        unfinished_score = clamp(unfinished_score - 0.35)
    preference_score = signal_score(preference_hits, base=0.64)
    procedural_score = signal_score(procedural_hits, base=0.66)
    correction_score = signal_score(correction_hits, base=0.72)
    confirmation_score = signal_score(confirmation_hits, base=0.72)
    importance_score = signal_score(importance_hits, base=0.65)

    weighted_signals = {
        "affect": 0.92 * affect_intensity,
        "unfinished": 0.86 * unfinished_score,
        "preference": 0.82 * preference_score,
        "procedure": 0.80 * procedural_score,
        "correction": 0.92 * correction_score,
        "confirmation": (0.88 if previous_assistant else 0.60) * confirmation_score,
        "importance": 0.80 * importance_score,
    }
    active_signals = [name for name, score in weighted_signals.items() if score >= 0.25]
    capture_score = max(weighted_signals.values(), default=0.0)
    capture_score = clamp(capture_score + min(0.16, max(0, len(active_signals) - 1) * 0.04))

    reasons: list[str] = []
    if affect_intensity:
        reasons.append("strong_affect")
    if unfinished_score:
        reasons.append("unfinished_or_future")
    if preference_score:
        reasons.append("preference_or_policy")
    if procedural_score:
        reasons.append("procedure")
    if correction_score:
        reasons.append("correction")
    if confirmation_score:
        reasons.append("confirmation")
    if importance_score:
        reasons.append("importance_expression")

    if procedural_score > 0.0 and procedural_score >= max(unfinished_score, preference_score, affect_intensity):
        memory_type = "procedural"
    elif unfinished_score > 0.0 and unfinished_score >= max(preference_score, affect_intensity):
        memory_type = "prospective"
    elif preference_score or correction_score or confirmation_score:
        memory_type = "semantic"
    else:
        memory_type = "episodic"

    quoted = excerpt(compact)
    if confirmation_score and previous_assistant:
        summary = (
            "ユーザーは直前のアシスタント発言を肯定した。"
            f"確認対象の要旨: 「{excerpt(previous_assistant, 160)}」"
        )
    elif correction_score:
        summary = f"ユーザーは以前の内容を訂正し、「{quoted}」と述べた。"
    elif unfinished_score:
        summary = f"ユーザーは今後の予定または未完了事項として「{quoted}」と述べた。"
    elif procedural_score:
        summary = f"ユーザーは手順または運用規則として「{quoted}」と述べた。"
    elif preference_score:
        summary = f"ユーザーは好みまたは方針として「{quoted}」と述べた。"
    elif affect_intensity:
        summary = f"ユーザーは「{quoted}」と強い表現で述べた。"
    else:
        summary = f"ユーザーは「{quoted}」と述べた。"

    emotion_tags: list[str] = []
    if positive_hits:
        emotion_tags.append("positive_expression")
    if negative_hits:
        emotion_tags.append("negative_expression")
    if surprise_hits:
        emotion_tags.append("surprise_expression")

    return {
        "eligible": bool(reasons),
        "raw_content": compact,
        "summary": summary,
        "candidate_memory_type": memory_type,
        "capture_score": capture_score,
        "unfinished_score": unfinished_score,
        "confirmation_score": confirmation_score,
        "correction_score": correction_score,
        "preference_score": preference_score,
        "procedural_score": procedural_score,
        "importance_score": importance_score,
        "affect_signal": {
            "valence": valence,
            "intensity": affect_intensity,
            "tags": emotion_tags,
            "source": "language_expression",
        },
        "reasons": reasons,
        "fingerprint": content_fingerprint(compact),
    }


def apply_repetition(analysis: dict[str, Any], repetition_score: float) -> dict[str, Any]:
    updated = dict(analysis)
    repetition = clamp(repetition_score)
    updated["repetition_score"] = repetition
    if repetition:
        updated["capture_score"] = clamp(max(float(updated.get("capture_score", 0.0)), 0.82 * repetition))
        reasons = list(updated.get("reasons") or [])
        if "repetition" not in reasons:
            reasons.append("repetition")
        updated["reasons"] = reasons
        updated["eligible"] = True
        if not analysis.get("eligible"):
            updated["summary"] = f"ユーザーは「{excerpt(str(analysis.get('raw_content', '')))}」と繰り返し述べた可能性がある。"
            updated["candidate_memory_type"] = "semantic"
    return updated
