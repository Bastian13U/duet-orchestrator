"""Small, non-streaming API clients with no tools, redirects, or implicit retries.

Keys come from the configured local credential resolver or an explicit value.
Errors deliberately omit response bodies, prompts, headers, and underlying URLs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import ssl
from typing import Protocol
import urllib.error
import urllib.request

from .credentials import get_key, validate_key, CredentialError


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_TOKENS = 1_000_000


class ProviderError(RuntimeError):
    """A sanitized provider failure; callers must retain the budget reservation."""


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int
    output_tokens: int


class Provider(Protocol):
    name: str

    def generate(self, system: str, prompt: str, schema: dict,
                 max_output_tokens: int) -> Reply: ...


def _tokens(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ProviderError("Provider returned invalid usage; reservation retained")
    return value


def _encode(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ProviderError("Invalid request data") from None


def request_bound(system: str, prompt: str, schema: dict) -> int:
    """Reserve one token per UTF-8 byte plus generous protocol/schema overhead.

    This is a deliberately conservative estimate, not a provider billing promise.
    The ledger blocks further calls if observed usage ever exceeds a reservation.
    """
    if not isinstance(system, str) or not isinstance(prompt, str) or not isinstance(schema, dict):
        raise ProviderError("Invalid request data")
    encoded = _encode({"system": system, "prompt": prompt, "schema": schema})
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ProviderError("Request exceeds the local size limit")
    return len(encoded) + 8192


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    # Endpoints are constants below; proxy environment variables are not honored.
    data = _encode(payload)
    if len(data) > MAX_REQUEST_BYTES:
        raise ProviderError("Request exceeds the local size limit")
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        request = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            **headers,
        }, method="POST")
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise ProviderError("Provider request failed; reservation retained")
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProviderError("Provider response exceeds the local size limit")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ProviderError("Provider returned an invalid response")
        return result
    except ProviderError:
        raise
    except Exception:
        # No retry: a timeout may have occurred after a billable generation.
        raise ProviderError("Provider request failed; reservation retained") from None


def _json_text(parts: list[str]) -> str:
    text = "".join(parts)
    try:
        if not isinstance(json.loads(text), dict):
            raise ValueError
    except (ValueError, TypeError):
        raise ProviderError("Provider did not return a JSON object") from None
    return text


class _BaseProvider:
    request_bound = staticmethod(request_bound)
    env_key: str

    def __init__(self, model: str, key: str | None = None, timeout: float = 60):
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ProviderError("A model identifier is required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ProviderError("Invalid provider timeout")
        try:
            secret = key if key is not None else get_key(self.env_key)
            if secret is not None:
                validate_key(secret)
        except CredentialError:
            raise ProviderError('API credential store is unavailable or invalid') from None
        if secret is None:
            raise ProviderError(f"Configure {self.env_key} securely in the environment, .env.local, or Windows Credential Manager")
        self.model = model
        self._key = secret
        self.timeout = timeout

    def _validate(self, system: str, prompt: str, schema: dict, max_output_tokens: int) -> None:
        request_bound(system, prompt, schema)
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_TOKENS:
            raise ProviderError("Invalid output token limit")


class OpenAIProvider(_BaseProvider):
    name = "openai"
    env_key = "OPENAI_API_KEY"

    def generate(self, system: str, prompt: str, schema: dict, max_output_tokens: int) -> Reply:
        self._validate(system, prompt, schema, max_output_tokens)
        result = _post("https://api.openai.com/v1/responses", {
            "model": self.model, "store": False,
            "input": [{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            "max_output_tokens": max_output_tokens,
            "text": {"format": {"type": "json_schema", "name": "agent_reply",
                                  "strict": True, "schema": schema}},
        }, {"Authorization": "Bearer " + self._key}, self.timeout)
        try:
            if result.get("status") != "completed" or result.get("error") or result.get("incomplete_details"):
                raise ProviderError("Provider refused or did not complete the response")
            parts = []
            for item in result["output"]:
                if item.get("type") == "reasoning":
                    continue
                if item.get("type") != "message" or item.get("role") != "assistant":
                    raise ProviderError("Provider returned an unexpected response item")
                if item.get("status") not in (None, "completed"):
                    raise ProviderError("Provider did not complete the response")
                for content in item["content"]:
                    if content.get("type") != "output_text" or not isinstance(content.get("text"), str):
                        raise ProviderError("Provider refused or returned unexpected content")
                    parts.append(content["text"])
            usage = result["usage"]
            return Reply(_json_text(parts), _tokens(usage["input_tokens"]),
                         _tokens(usage["output_tokens"]))
        except ProviderError:
            raise
        except (KeyError, TypeError, AttributeError):
            raise ProviderError("Provider returned an invalid response") from None


class AnthropicProvider(_BaseProvider):
    name = "anthropic"
    env_key = "ANTHROPIC_API_KEY"

    def generate(self, system: str, prompt: str, schema: dict, max_output_tokens: int) -> Reply:
        self._validate(system, prompt, schema, max_output_tokens)
        result = _post("https://api.anthropic.com/v1/messages", {
            "model": self.model, "max_tokens": max_output_tokens,
            "system": system, "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }, {"x-api-key": self._key, "anthropic-version": "2023-06-01"}, self.timeout)
        try:
            details = result.get("stop_details")
            if result.get("stop_reason") != "end_turn" or result.get("type") != "message" or result.get("role") != "assistant" or (isinstance(details, dict) and details.get("type") == "refusal"):
                raise ProviderError("Provider refused or did not complete the response")
            parts = []
            for content in result["content"]:
                if content.get("type") != "text" or not isinstance(content.get("text"), str):
                    raise ProviderError("Provider returned unexpected content")
                parts.append(content["text"])
            usage = result["usage"]
            input_tokens = _tokens(usage["input_tokens"])
            input_tokens += _tokens(usage.get("cache_read_input_tokens", 0))
            # We never request cache creation, whose tariff differs from normal input.
            if _tokens(usage.get("cache_creation_input_tokens", 0)):
                raise ProviderError("Unexpected cache creation usage; reservation retained")
            return Reply(_json_text(parts), _tokens(input_tokens), _tokens(usage["output_tokens"]))
        except ProviderError:
            raise
        except (KeyError, TypeError, AttributeError):
            raise ProviderError("Provider returned an invalid response") from None
