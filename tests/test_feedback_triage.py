"""New fictional feedback examples only; no deployment data or model calls.

DEVELOPMENT reproduces the original twelve diagnostic examples. HOLDOUT was
written separately before implementing v0.3 and is scored independently; neither
small, selected collection estimates production accuracy or calibration.
"""
from __future__ import annotations

import json
import unittest

from v02.policy import detect_corrections


DEVELOPMENT = (
    ("P1", "正しくは火曜です。", ("content_correction",)),
    ("P2", "それを言ったのは私ではない。", ("attribution_correction",)),
    ("P3", "その一件だけです。一般化しないでください。", ("overgeneralization",)),
    ("P4", "今は別の案件の話です。", ("context_mismatch",)),
    ("N1", "「正しくは火曜です」と作中人物が言った。", ()),
    ("N2", "```\ncorrection: demo date\n```", ()),
    ("N3", "さすが、完璧ですね（笑）。", ()),
    ("N4", "はいはい、本当にすばらしいですね。", ()),
    ("N5", "それは訂正という意味ですか？", ()),
    ("N6", "日付が違うのですか？", ()),
    ("N7", "最悪だね。", ()),
    ("N8", "訂正ではありません。確認だけです。", ()),
)

HOLDOUT = (
    ("H01", "その発言を述べたのは別の人です。", ("attribution_correction",)),
    ("H02", "I didn't say the beacon was orange.", ("attribution_correction",)),
    ("H03", "毎回同じとは限らない。", ("overgeneralization",)),
    ("H04", "Please don't generalize from one fictional run.", ("overgeneralization",)),
    ("H05", "今回は違うプロジェクトの話です。", ("context_mismatch",)),
    ("H06", "Wrong context. We are discussing a separate mock project.", ("context_mismatch",)),
    ("H07", "時間は9時ではなく11時です。", ("content_correction",)),
    ("H08", "That is incorrect; the demo marker is purple.", ("content_correction",)),
    ("H09", "誤りですという文を例として使います。", ()),
    ("H10", "The mock incident report stated that the wrong date was entered.", ()),
    ("H11", "仮に毎回違うなら、一般化しない方がよい。", ()),
    ("H12", "Suppose the fixture used the wrong context.", ()),
    ("H13", "これは訂正ではなく、質問の例です。", ()),
    ("H14", "This is not a correction: please explain the notation.", ()),
    ("H15", "今は別の案件の話なのでしょうか。", ()),
    ("H16", "Do you mean that I didn't say this?", ()),
    ("H17", "『いつもではない』という架空の台詞です。正しくは土曜です。どの例を使いますか？",
     ("content_correction",)),
    ("H18", "If needed, print correction: demo value. I did not say this. Is the wrong date relevant?",
     ("attribution_correction",)),
)


def score(cases):
    """Only aggregate artificial fixture outcomes; do not log source text."""
    result = {"total": len(cases), "exact": 0, "tp": 0, "tn": 0, "fp": 0, "fn": 0}
    for _, text, expected in cases:
        actual = detect_corrections(text)["categories"]
        result["exact"] += set(actual) == set(expected)
        key = ("tp" if expected else "fp") if actual else ("fn" if expected else "tn")
        result[key] += 1
    return result


class FeedbackTriageTests(unittest.TestCase):
    def assert_categories(self, text, expected):
        result = detect_corrections(text)
        self.assertEqual(set(result["categories"]), set(expected))
        self.assertEqual(result["status"], "weak_signal" if expected else "unknown")
        self.assertEqual(result["detection_confidence"], .65 if expected else None)
        self.assertEqual(result["confidence_status"], "uncalibrated_heuristic")
        self.assertEqual(result["detector_version"], "correction-rules.v0.3")
        self.assertEqual(len(result["evidence_spans"]), len(result["categories"]))
        for span in result["evidence_spans"]:
            self.assertLess(span["start"], span["end"])
            self.assertGreaterEqual(span["start"], 0)
            self.assertLessEqual(span["end"], len(text))
        return result

    def test_original_development_collection(self):
        for name, text, expected in DEVELOPMENT:
            with self.subTest(name=name):
                self.assert_categories(text, expected)

    def test_separately_fixed_holdout_collection(self):
        for name, text, expected in HOLDOUT:
            with self.subTest(name=name):
                self.assert_categories(text, expected)

    def test_direct_english_and_japanese_corrections(self):
        for text, label in (
            ("I did not say that.", "attribution_correction"),
            ("Not always.", "overgeneralization"),
            ("Don't generalize.", "overgeneralization"),
            ("It is a different project.", "context_mismatch"),
            ("That's incorrect.", "content_correction"),
            ("Correction: use the demo value.", "content_correction"),
            ("訂正します。", "content_correction"),
            ("いつもではない。", "overgeneralization"),
        ):
            with self.subTest(text=text):
                self.assert_categories(text, [label])

    def test_questions_are_unknown_with_or_without_question_mark(self):
        for text in (
            "日付が違うのですか", "訂正なのかどうか確認したい。", "一般化しないという意味でしょうか。",
            "それを言ったのは私ではないですか？", "今は別の話ですか。",
            "Is that a correction", "Do you mean not always", "Was that the wrong date?",
            "Isn't that the wrong context?", "I wonder whether that's incorrect.",
            "Correction: could you explain the new value", "That's incorrect, right?",
        ):
            with self.subTest(text=text):
                self.assert_categories(text, [])

    def test_correction_denial_is_not_feedback(self):
        for text in (
            "訂正ではない。", "訂正じゃありません。", "訂正はありません。", "訂正は不要です。",
            "訂正するつもりはありません。", "訂正しません。",
            "日付が違うわけではありません。", "正しくは月曜だとは思いません。",
            "This isn't a correction: just a follow-up.", "No correction: thanks.",
            "I am not making a correction: this is an explanation.", "It is not the wrong date.",
            "I don't think that's incorrect.", "It's not true that that's incorrect.",
        ):
            with self.subTest(text=text):
                self.assert_categories(text, [])

    def test_hypothetical_and_reported_content_stays_unknown(self):
        for text in (
            "もし日付が違うなら訂正してください。", "例えば、正しくは月曜です。", "正しくは月曜だとしたら困ります。",
            "訂正があれば教えてください。", "日付が違うと聞きました。", "日付が違うらしいです。",
            "教材には一般化しないと書かれています。", "例：\n正しくは日曜です。",
            "If that's incorrect, ask the fictional editor.", "For example, not always.",
            "The example says wrong context.", "The note reports a correction: use demo.",
            "I heard that the note has the wrong date.", "I read that it's a different project.",
            "The script says:\nThat's incorrect.", "E.g. correction: use demo.",
            "Perhaps that's incorrect.", "That could be the wrong date.",
        ):
            with self.subTest(text=text):
                self.assert_categories(text, [])

    def test_quotations_code_and_unclosed_code_preserve_line_boundaries(self):
        for text in (
            "'that's incorrect' is a fictional line.", "‘don't generalize’ is a quotation.",
            '“wrong date” is a quotation.', "`訂正` is a field name.",
            "```demo\ncorrection: simulated value", "> Not always\n> That's incorrect.",
        ):
            with self.subTest(text=text):
                self.assert_categories(text, [])

    def test_mixed_sentences_keep_only_direct_evidence_offsets(self):
        for text, expected in (
            ("🙂それは訂正ですか？\n正しくは木曜です。例えば、今は別の話です。", "正しくは"),
            ("The note says that's incorrect.\nThat's incorrect. Is it the wrong date?", "That's incorrect"),
            ("「正しくは金曜？」という台詞です。\n正しくは土曜です。", "正しくは"),
            ("```\ncorrection: example\n```\nCorrection: use demo. What changed?", "Correction:"),
            ("訂正ではありません。\n日付が違います。訂正という意味ですか？", "日付が違"),
        ):
            with self.subTest(text=text):
                result = self.assert_categories(text, ["content_correction"])
                span = result["evidence_spans"][0]
                self.assertEqual(text[span["start"]:span["end"]], expected)
                self.assertEqual(span["start"], text.rfind(expected))

    def test_unknown_before_positive_does_not_consume_first_match(self):
        text = "If not always, ask again. Not always. Is that the wrong context? Wrong context."
        result = self.assert_categories(text, ["overgeneralization", "context_mismatch"])
        self.assertEqual([text[s["start"]:s["end"]] for s in result["evidence_spans"]],
                         ["Not always", "Wrong context"])

    def test_question_within_same_sentence_is_conservatively_unknown(self):
        self.assert_categories("Correction: use demo; is that what you meant?", [])
        self.assert_categories("正しくは日曜ですが、そういう意味ですか？", [])


if __name__ == "__main__":
    print(json.dumps({"development": score(DEVELOPMENT), "holdout": score(HOLDOUT)}, ensure_ascii=False))
    unittest.main()
