import copy
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from duet.providers import (AnthropicProvider, OpenAIProvider, ProviderError,
                            MAX_RESPONSE_BYTES, _NoRedirect, _post, request_bound)


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
OPENAI_REPLY = {
    "status": "completed", "output": [{"type": "message", "role": "assistant",
       "status": "completed", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
    "usage": {"input_tokens": 30, "output_tokens": 6},
}
ANTHROPIC_REPLY = {"type": "message", "role": "assistant", "stop_reason": "end_turn",
                   "content": [{"type": "text", "text": '{"ok":true}'}],
                   "usage": {"input_tokens": 30, "output_tokens": 6}}


class ProviderTests(unittest.TestCase):
    def test_openai_wire_format_and_usage(self):
        with patch("duet.providers._post", return_value=OPENAI_REPLY) as post:
            reply = OpenAIProvider("test-model", key="test-key").generate("system", "prompt", SCHEMA, 300)
        endpoint, body, headers, timeout = post.call_args.args
        self.assertEqual(endpoint, "https://api.openai.com/v1/responses")
        self.assertFalse(body["store"])
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["schema"], SCHEMA)
        self.assertEqual(body["max_output_tokens"], 300)
        self.assertNotIn("tools", body)
        self.assertEqual((reply.input_tokens, reply.output_tokens), (30, 6))

    def test_anthropic_wire_format_and_usage(self):
        with patch("duet.providers._post", return_value=ANTHROPIC_REPLY) as post:
            reply = AnthropicProvider("test-model", key="test-key").generate("system", "prompt", SCHEMA, 300)
        endpoint, body, headers, timeout = post.call_args.args
        self.assertEqual(endpoint, "https://api.anthropic.com/v1/messages")
        self.assertEqual(body["output_config"]["format"]["schema"], SCHEMA)
        self.assertEqual(body["max_tokens"], 300)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("tools", body)
        self.assertEqual(reply.text, '{"ok":true}')

    def test_anthropic_cached_input_is_counted(self):
        result = copy.deepcopy(ANTHROPIC_REPLY)
        result["usage"]["cache_read_input_tokens"] = 70
        with patch("duet.providers._post", return_value=result):
            reply = AnthropicProvider("test-model", key="test-key").generate("s", "p", SCHEMA, 300)
        self.assertEqual(reply.input_tokens, 100)

    def test_anthropic_unexpected_cache_creation_fails_closed(self):
        result = copy.deepcopy(ANTHROPIC_REPLY)
        result["usage"]["cache_creation_input_tokens"] = 10
        with patch("duet.providers._post", return_value=result), self.assertRaises(ProviderError):
            AnthropicProvider("test-model", key="test-key").generate("s", "p", SCHEMA, 300)

    def test_bad_usage_fails_closed(self):
        for provider_type, baseline in [(OpenAIProvider, OPENAI_REPLY), (AnthropicProvider, ANTHROPIC_REPLY)]:
            for value in [True, -1, 1.5, "3", None, float("inf"), 10**13]:
                with self.subTest(provider=provider_type.name, value=value):
                    result = copy.deepcopy(baseline)
                    result["usage"]["input_tokens"] = value
                    with patch("duet.providers._post", return_value=result), self.assertRaises(ProviderError):
                        provider_type("test-model", key="test-key").generate("s", "p", SCHEMA, 300)

    def test_openai_incomplete_and_refusal_fail_closed(self):
        incomplete = copy.deepcopy(OPENAI_REPLY)
        incomplete["status"] = "incomplete"
        refusal = copy.deepcopy(OPENAI_REPLY)
        refusal["output"][0]["content"] = [{"type": "refusal", "refusal": "sensitive text"}]
        for result in [incomplete, refusal, {}, {"status": "completed", "output": None}]:
            with patch("duet.providers._post", return_value=result), self.assertRaises(ProviderError):
                OpenAIProvider("test-model", key="test-key").generate("s", "p", SCHEMA, 300)

    def test_anthropic_incomplete_and_refusal_fail_closed(self):
        for stop in ["max_tokens", "refusal", "tool_use", "pause_turn", None]:
            result = copy.deepcopy(ANTHROPIC_REPLY)
            result["stop_reason"] = stop
            with patch("duet.providers._post", return_value=result), self.assertRaises(ProviderError):
                AnthropicProvider("test-model", key="test-key").generate("s", "p", SCHEMA, 300)
        result = copy.deepcopy(ANTHROPIC_REPLY)
        result["stop_details"] = {"type": "refusal"}
        with patch("duet.providers._post", return_value=result), self.assertRaises(ProviderError):
            AnthropicProvider("test-model", key="test-key").generate("s", "p", SCHEMA, 300)

    def test_missing_key_is_named_but_never_leaked(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderError, "OPENAI_API_KEY"):
                OpenAIProvider("test-model")
        with self.assertRaises(ProviderError) as caught:
            AnthropicProvider("test-model", key="SECRET\nVALUE")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_request_bound_includes_unicode_and_schema(self):
        simple = request_bound("s", "p", {})
        large = request_bound("s", "😀" * 100, SCHEMA)
        self.assertGreaterEqual(large - simple, 399)
        self.assertGreater(large, 8192)
        with self.assertRaises(ProviderError):
            request_bound("s", "x" * (1024 * 1024), {})

    def test_invalid_limits_fail_before_network(self):
        provider = OpenAIProvider("test-model", key="test-key")
        with patch("duet.providers._post") as post:
            for value in [True, 0, -1, float("inf"), 1.5]:
                with self.assertRaises(ProviderError):
                    provider.generate("s", "p", SCHEMA, value)
            post.assert_not_called()

    def test_transport_sanitizes_errors_and_does_not_retry(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("SUPERSECRET request body")
        with patch("duet.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError) as caught:
                _post("https://api.openai.com/v1/responses", {}, {}, 10)
        self.assertNotIn("SUPERSECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        opener.open.assert_called_once()

    def test_transport_bounds_response(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"x" * (MAX_RESPONSE_BYTES + 1)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("duet.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError):
                _post("https://api.openai.com/v1/responses", {}, {}, 10)
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)

    def test_redirects_are_never_followed(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://attacker.invalid"))


if __name__ == "__main__":
    unittest.main()
