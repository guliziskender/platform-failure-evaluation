"""`pfa diagnose` with and without an AI provider, on a replayed evidence file."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pfa import cli
from pfa.ai import providers

from .conftest import EVIDENCE_FIXTURES, FakeProvider, llm_reply

CRASH = str(EVIDENCE_FIXTURES / "crash_loop_backoff.json")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("PFA_PROVIDER", "PFA_SEND_DATA", "PFA_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def run(*args: str):
    result = CliRunner().invoke(cli.app, ["diagnose", "--from-evidence", CRASH, *args])
    assert result.exit_code == 0, result.output
    return result


def use_provider(monkeypatch, provider) -> None:
    monkeypatch.setattr(providers, "make_provider", lambda settings: provider)


def test_ai_answer_is_shown_with_its_model(monkeypatch):
    provider = FakeProvider(llm_reply())
    use_provider(monkeypatch, provider)

    out = run().stdout
    assert "AI (fake-model)" in out
    assert "DATABASE_URL" in out
    assert "Rule findings" in out  # rules are still shown next to the AI answer
    assert len(provider.calls) == 1


def test_ai_answer_as_json(monkeypatch):
    use_provider(monkeypatch, FakeProvider(llm_reply()))
    d = json.loads(run("-o", "json").stdout)
    assert d["diagnosis"]["source"] == "llm"
    assert d["diagnosis"]["model"] == "fake-model"
    assert d["notes"] == []


def test_no_llm_never_builds_a_provider(monkeypatch):
    def boom(settings):
        raise AssertionError("provider must not be created with --no-llm")

    monkeypatch.setattr(providers, "make_provider", boom)
    d = json.loads(run("--no-llm", "-o", "json").stdout)
    assert d["diagnosis"]["source"] == "rules"
    assert d["notes"] == []


def test_without_provider_answers_from_rules_and_says_why():
    d = json.loads(run("-o", "json").stdout)
    assert d["diagnosis"]["source"] == "rules"
    assert "No AI provider configured" in d["notes"][0]


def test_hosted_provider_without_send_data_sends_nothing(monkeypatch):
    monkeypatch.setenv("PFA_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    d = json.loads(run("-o", "json").stdout)
    assert d["diagnosis"]["source"] == "rules"
    assert "PFA_SEND_DATA=1" in d["notes"][0]


def test_provider_failure_falls_back_with_a_note(monkeypatch):
    from pfa.ai.providers import ProviderError

    use_provider(monkeypatch, FakeProvider(ProviderError("Anthropic API timed out")))
    out = run().stdout
    assert "rules only" in out
    assert "note: AI unavailable (Anthropic API timed out)" in out


def test_show_prompt_sends_nothing(monkeypatch):
    def boom(settings):
        raise AssertionError("--show-prompt must not create a provider")

    monkeypatch.setattr(providers, "make_provider", boom)
    result = run("--show-prompt")
    assert "## Classification (deterministic)" in result.stdout
    assert "Nothing was sent" in result.stderr


def test_show_prompt_json():
    p = json.loads(run("--show-prompt", "-o", "json").stdout)
    assert set(p) == {"system", "user", "est_tokens", "trimmed"}
    assert "Use only the evidence given" in p["system"]
