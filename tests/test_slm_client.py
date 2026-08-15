from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from slm_client import StructuredSLMClient


class StructuredSLMClientTests(unittest.TestCase):
    def test_ollama_chat_sends_schema_and_persistent_keep_alive(self) -> None:
        response = Mock()
        response.read.return_value = json.dumps(
            {
                "model": "qwen3.5:4b",
                "message": {"content": '{"claims":[]}'},
                "load_duration": 1000000,
                "prompt_eval_count": 12,
                "eval_count": 5,
            }
        ).encode("utf-8")
        schema = {
            "type": "object",
            "properties": {"claims": {"type": "array"}},
            "required": ["claims"],
        }
        client = StructuredSLMClient(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:4b",
            keep_alive=-1,
        )

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = client.structured_chat(
                messages=[{"role": "user", "content": "Structure this."}],
                schema=schema,
            )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(payload["format"], schema)
        self.assertEqual(payload["keep_alive"], -1)
        self.assertFalse(payload["stream"])
        self.assertEqual(result["content"], '{"claims":[]}')
        self.assertEqual(result["provider"], "ollama")


if __name__ == "__main__":
    unittest.main()
