"""Pure, bounded policy functions. No persistence, model calls, or truth updates."""
from __future__ import annotations

import re
from datetime import timedelta

from .contracts import now, timestamp


def terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_]{2,}|[\u3040-\u30ff\u3400-\u9fff]+", text.casefold()))
    # Japanese lacks spaces; reuse lexical matching rather than treating a sentence as one token.
    for word in tuple(words):
        if re.search(r"[\u3040-\u9fff]", word) and len(word) > 3:
            words.update(word[i:i + 3] for i in range(len(word) - 2))
    return words


CORRECTIONS = {
    "attribution_correction": r"(?:言った|述べた|発言した).{0,16}(?:私ではない|私じゃない|別の人)|(?:それ|その話).{0,12}(?:私の発言では|私が言った).{0,5}(?:ない|違)|(?:i (?:did not|didn't) say|(?:someone else|my brother|my sister) said)",
    "overgeneralization": r"(?:いつも|毎回|全員|すべて).{0,12}(?:ではない|じゃない|限らない)|(?:その一件|その時だけ|一般化しない|not always|just that once|don't generalize)",
    "context_mismatch": r"(?:今|今回).{0,12}(?:別の|違う).{0,10}(?:話|案件|プロジェクト)|(?:別件の話|今はその話では|wrong context|different project|not what we're discussing)",
    "content_correction": r"(?:訂正|正しくは|誤りです|ではなく|じゃなく|日付が違|日時が違)|(?:that's incorrect|that is incorrect|correction:|wrong date|not .{1,35} but)",
}


CORRECTION_DETECTOR_VERSION = "correction-rules.v0.3"

# These are abstention guards, not semantic truth/intent judgments. In particular,
# a corrective question or reported attribution may be missed deliberately. Keep
# the old weak-signal score: this lexical triage has not calibrated probabilities.
_QUESTION = re.compile(
    r"[?？]|かどうか|(?:ですか|ますか|でしょうか|なのか|のか|かな|かしら)[。.!！\s]*$"
    r"|(?:^|[:：])\s*(?:am|is|are|was|were|do|does|did|can|could|would|should|will|have|has|had|may|might|"
    r"what|why|how|where|when|which|who|whether)\b"
    r"|^\s*[a-z]+n['’]t\s+(?:i|you|we|they|he|she|it|that|this|the)\b"
    r"|\b(?:wonder(?:ing)?|ask(?:ing|ed)?)\s+(?:if|whether|why|what|how)\b", re.I)
_DENIED_CORRECTION = re.compile(
    r"訂正(?:で(?:は|も)|じゃ)(?:な(?:い|く|かった)|ありません)"
    r"|訂正(?:[はがを])?(?:不要|無用|なし|ない|ありません|し(?:ない|ません))"
    r"|訂正(?:を?する|を求める)(?:つもり|必要|意図)(?:は|が)?(?:ない|ありません)"
    r"|(?:わけ|こと|意味|の)(?:では|じゃ)(?:な(?:い|く)|ありません)"
    r"|とは(?:思わない|思いません|考えない|考えません)"
    r"|\b(?:not|isn't|wasn't)\s+(?:an?\s+)?correction\b"
    r"|\b(?:no|without)\s+corrections?\b"
    r"|\bnot\s+(?:making|offering|requesting|issuing|providing)\s+(?:a\s+)?correction\b"
    r"|\b(?:not|isn't|wasn't)\s+(?:the\s+)?(?:wrong|incorrect|true|the case)\b"
    r"|\b(?:not|don't|didn't|never)\s+(?:think|believe|mean|claim|suggest|imply|saying)\b", re.I)
_HYPOTHETICAL_OR_REPORTED = re.compile(
    r"もし|仮に|仮定|例えば|たとえば|例として|例文|(?:^|[\s、,])例[:：]|(?:の|という)例"
    r"|と(?:すれば|したら|すると|するなら)|だったら|であれば|れば|ならば|かもしれ"
    r"|によ(?:ると|れば)|(?:と|って)(?:いう|言う|言っ|述べ|書|読|聞|報告|説明|伝え|され)"
    r"|(?:という|といった)(?:文|文章|発言|主張|台詞|せりふ|表現|記述|記載|意味|例)"
    r"|(?:そう|らしい)です"
    r"|\b(?:if|unless|suppose|supposing|assuming|hypothetical(?:ly)?|for example|for instance|"
    r"examples?|would|could|might|maybe|perhaps|said|says|heard|read|reads|reported|reports|wrote|writes|stated|states|"
    r"claimed|claims|according to)\b|\be\.g\.", re.I)
_SENTENCE_END = re.compile(r"[。！？!?]+|\r?\n|\.(?=\s|$)")
_ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|mr|mrs|ms|dr|vs|etc|[a-z])\.$", re.I)
_QUOTED = re.compile(
    r'```.*?(?:```|$)|`[^`\r\n]*`|「[^」]*」|『[^』]*』|"[^"\r\n]*"|“[^”]*”'
    r"|‘[^\r\n]*?’(?!\w)|(?<!\w)'[^\r\n]*?'(?!\w)", re.S)


def _mask_feedback_quotes(text: str) -> str:
    # Preserve both Unicode-codepoint offsets and physical line boundaries.
    def blank(match):
        return re.sub(r"[^\r\n]", " ", match.group())
    clean = _QUOTED.sub(blank, text)
    return re.sub(r"(?m)^[ \t]*>[^\r\n]*", blank, clean)


def _feedback_sentences(clean: str):
    start = 0
    for match in _SENTENCE_END.finditer(clean):
        # A colon introduces the following example/report, even across a newline.
        # Semicolons do not delimit sentences: a later question still abstains.
        if "\n" in match.group() and clean[start:match.start()].rstrip().endswith((":", "：")):
            continue
        if match.group() == "." and _ABBREVIATION.search(clean[max(start, match.end() - 16):match.end()]):
            continue
        yield start, clean[start:match.end()]
        start = match.end()
    if start < len(clean):
        yield start, clean[start:]


def _direct_feedback_sentences(clean: str):
    for offset, sentence in _feedback_sentences(clean):
        # Strip formatting only for the guards; searches retain original offsets.
        guard_text = sentence.lstrip(" \t*_#")
        if not any(guard.search(guard_text) for guard in
                   (_QUESTION, _DENIED_CORRECTION, _HYPOTHETICAL_OR_REPORTED)):
            yield offset, sentence


def detect_corrections(text: str) -> dict:
    """Find weak direct correction cues; ambiguous/quoted sentences abstain.

    This does not infer a corrected memory, truth, confidence, or a reward from
    silence. Irony, unmarked discourse context, and novel paraphrases remain
    unsupported; callers must keep policy effects separately controlled.
    """
    clean = _mask_feedback_quotes(text).replace("’", "'")
    sentences = tuple(_direct_feedback_sentences(clean))
    labels, spans = [], []
    for label, pattern in CORRECTIONS.items():
        for offset, sentence in sentences:
            match = re.search(pattern, sentence, flags=re.I)
            if match:
                labels.append(label)
                # Keep one exact evidence span per category, as in v0.2. An earlier
                # ambiguous/quoted occurrence must not consume the accepted one.
                spans.append({"start": offset + match.start(), "end": offset + match.end()})
                break
    return {"categories": labels, "evidence_spans": spans,
            "detection_confidence": 0.65 if labels else None,
            "confidence_status": "uncalibrated_heuristic", "detector_version": CORRECTION_DETECTOR_VERSION,
            "status": "weak_signal" if labels else "unknown"}


def expires_in(seconds: int) -> str:
    return (timestamp(now()) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def active(data: dict, at: str | None = None) -> bool:
    return bool(data.get("expiry")) and timestamp(data["expiry"]) > timestamp(at or now())


def sandbox_transition(previous: dict | None, request: dict) -> dict:
    previous = previous or {"mode": "objective_default", "gain": 0.0}
    expired = timestamp(request["expiry"]) <= timestamp(now())
    enabled = request["enabled"] and not expired
    danger = (request["safe_as_fiction"] is not True and request["current_real_world_action"] is True
              and request["current_loss_possible"] is True)
    can_enter = (enabled and request["safe_as_fiction"] is True
                 and request["current_real_world_action"] is False)
    gain = float(previous["gain"]) if active(previous) else 0.0
    mode = previous["mode"]
    unresolved_risk = previous.get("unresolved_current_risk", False)
    if danger:
        unresolved_risk = True
    elif can_enter and request["evidence_refs"]:
        unresolved_risk = False
    if not enabled:
        mode, gain = "objective_support" if unresolved_risk else "objective_default", 0.0
    elif danger:
        gain = 0.0 if request["urgent"] else max(0.0, gain - 0.5)
        mode = "objective_support" if gain == 0 else "exiting"
    elif mode in {"exiting", "objective_support", "reentry_check"}:
        # Reentry stages require explicit evidence-backed assessments, not elapsed time.
        if can_enter and request["evidence_refs"]:
            mode = "reentry_check" if mode != "reentry_check" else "reentering"
            gain = 0.0
        else:
            mode, gain = "objective_support", max(0.0, gain - 0.5)
    elif can_enter:
        gain = min(1.0, gain + 0.25)
        mode = "sandbox_active" if gain == 1.0 else "reentering"
    else:
        mode, gain = "boundary_check", min(gain, 0.25)
    objective_required = danger or mode in {"exiting", "objective_support", "reentry_check", "boundary_check"}
    return {"mode": mode, "gain": gain, "unresolved_current_risk": unresolved_risk,
            "target_gain": 0.0 if objective_required or not enabled else 1.0,
            "objective_support_required": objective_required,
            "risk": {k: request[k] for k in ("loss_magnitude", "controllability", "reversibility", "cognitive_influence")},
            "caution_gain": request["cognitive_influence"],
            "assessment": {k: request[k] for k in ("safe_as_fiction", "current_real_world_action", "current_loss_possible")}}
