"""Provider adapters against mocked HTTP (no network), and the send_data switch."""

from __future__ import annotations

import json

import anthropic
import httpx
import httpx2
import pytest

from pfa.ai.llm import ANSWER_SCHEMA
from pfa.ai.providers import ProviderConfigError, ProviderError, data_flow, make_provider
from pfa.ai.providers.anthropic import AnthropicProvider
from pfa.ai.providers.ollama import OllamaProvider
from pfa.config import Settings

# --- Anthropic ------------------------------------------------------------------------------


def _message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def _anthropic(handler) -> AnthropicProvider:
    client = anthropic.Anthropic(
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    return AnthropicProvider(model="claude-test", client=client)


def test_anthropic_sends_schema_as_structured_output():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx2.Response(200, json=_message('{"ok": true}'))

    reply = _anthropic(handler).complete("system text", "user text", ANSWER_SCHEMA)

    assert reply == '{"ok": true}'
    assert seen["model"] == "claude-test"
    assert seen["system"] == "system text"
    assert seen["messages"] == [{"role": "user", "content": "user text"}]
    assert seen["output_config"] == {"format": {"type": "json_schema", "schema": ANSWER_SCHEMA}}


def _error(status: int, kind: str):
    body = {"type": "error", "error": {"type": kind, "message": "details"}}
    return lambda request: httpx2.Response(status, json=body)


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (_error(429, "rate_limit_error"), "rate limit"),
        (_error(401, "authentication_error"), "rejected the API key"),
        (_error(500, "api_error"), "error 500"),
        (lambda r: httpx2.Response(200, json=_message("", "refusal")), "declined"),
        (lambda r: httpx2.Response(200, json=_message('{"a"', "max_tokens")), "cut off"),
        (lambda r: httpx2.Response(200, json=_message("   ")), "empty reply"),
    ],
)
def test_anthropic_failures_become_provider_errors(handler, message):
    with pytest.raises(ProviderError, match=message):
        _anthropic(handler).complete("s", "u", ANSWER_SCHEMA)


def test_anthropic_connection_error():
    def handler(request):
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(ProviderError, match="cannot reach"):
        _anthropic(handler).complete("s", "u", ANSWER_SCHEMA)


# --- Ollama -------------------------------------------------------------------------------


def _ollama(*responses) -> tuple[OllamaProvider, list[dict]]:
    queue, seen = list(responses), []

    def handler(request):
        seen.append(json.loads(request.content))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaProvider(model="qwen-test", client=client, backoff=0), seen


def _chat(text: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}})


def test_ollama_sends_schema_as_format():
    provider, seen = _ollama(_chat('{"ok": true}'))
    assert provider.complete("sys", "usr", ANSWER_SCHEMA) == '{"ok": true}'
    body = seen[0]
    assert body["format"] == ANSWER_SCHEMA
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "sys"}


def test_ollama_retries_server_errors_then_succeeds():
    provider, seen = _ollama(httpx.Response(503), httpx.Response(500), _chat("{}"))
    assert provider.complete("s", "u", {}) == "{}"
    assert len(seen) == 3


def test_ollama_gives_up_after_retries():
    provider, _ = _ollama(*[httpx.Response(503, text="busy")] * 3)
    with pytest.raises(ProviderError, match="503"):
        provider.complete("s", "u", {})


def test_ollama_missing_model_says_how_to_fix():
    provider, _ = _ollama(httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ProviderError, match="ollama pull qwen-test"):
        provider.complete("s", "u", {})


def test_ollama_not_running():
    request = httpx.Request("POST", "http://localhost:11434/api/chat")
    errors = [httpx.ConnectError("refused", request=request)] * 3
    provider, _ = _ollama(*errors)
    with pytest.raises(ProviderError, match="cannot reach Ollama"):
        provider.complete("s", "u", {})


# --- choosing a provider, and the send_data switch -----------------------------------------


def test_no_provider_means_rules_only():
    assert make_provider(Settings()) is None


def test_hosted_provider_needs_send_data():
    key = {"anthropic_api_key": "test-key"}
    with pytest.raises(ProviderConfigError, match="send_data is off"):
        make_provider(Settings(provider="anthropic", **key))
    provider = make_provider(
        Settings(provider="anthropic", send_data=True, model="claude-x", **key)
    )
    assert (provider.name, provider.model, provider.local) == ("anthropic", "claude-x", False)


def test_anthropic_needs_a_key():
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_API_KEY"):
        make_provider(Settings(provider="anthropic", send_data=True))


def test_local_ollama_needs_no_send_data():
    provider = make_provider(Settings(provider="ollama"))
    assert (provider.destination, provider.local) == ("localhost:11434", True)


def test_remote_ollama_needs_send_data():
    remote = Settings(provider="ollama", ollama_url="http://gpu-box.internal:11434")
    with pytest.raises(ProviderConfigError, match="PFA_SEND_DATA"):
        make_provider(remote)


def test_unknown_provider():
    with pytest.raises(ProviderConfigError, match="anthropic, ollama"):
        make_provider(Settings(provider="gemini"))


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (Settings(), "rules only. Nothing leaves this machine"),
        (Settings(provider="ollama"), "at localhost:11434. Nothing leaves this machine"),
        (Settings(provider="anthropic"), "set but not used: not sending evidence"),
        (
            Settings(provider="anthropic", send_data=True, anthropic_api_key="k"),
            "Redacted evidence is sent to api.anthropic.com",
        ),
    ],
    ids=["none", "local", "blocked", "hosted"],
)
def test_data_flow_sentence(settings, expected):
    assert expected in data_flow(settings)[1]
