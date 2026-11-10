"""The AI answer path with a fake provider: validation, repair, sanity checks, fallback."""

from __future__ import annotations

import json

import pytest

from pfa.ai.llm import ANSWER_SCHEMA, InvalidAnswer, LLMAnswer, explain, parse_answer
from pfa.ai.providers.base import ProviderError
from pfa.models import FailureType

from .conftest import FakeProvider, llm_reply, replay_report


@pytest.fixture
def crash():
    return replay_report("crash_loop_backoff")


def test_valid_reply_becomes_the_diagnosis(crash):
    provider = FakeProvider(llm_reply())
    report = explain(crash, provider)

    d = report.diagnosis
    assert (d.source, d.model) == ("llm", "fake-model")
    assert d.failure_type is FailureType.CRASH_LOOP
    assert d.confidence == "high"
    assert "DATABASE_URL" in d.likely_cause
    assert d.rule_hits == [h.rule for h in crash.rule_hits]  # rules still credited
    assert report.rule_hits == crash.rule_hits
    assert report.notes == []
    assert provider.calls[0]["schema"] is ANSWER_SCHEMA


def test_invalid_reply_gets_one_repair_call(crash):
    provider = FakeProvider("Sure! Here is the diagnosis: it crashed.", llm_reply())
    report = explain(crash, provider)

    assert report.diagnosis.source == "llm"
    assert len(provider.calls) == 2
    repair = provider.calls[1]["user"]
    assert "Your previous reply was invalid" in repair
    assert "not JSON" in repair


def test_reply_outside_the_schema_is_repaired(crash):
    provider = FakeProvider(llm_reply(confidence="very high"), llm_reply())
    explain(crash, provider)
    assert "confidence" in provider.calls[1]["user"]


def test_invalid_twice_falls_back_to_rules(crash):
    provider = FakeProvider("not json", '{"failure_type": "crash_loop_backoff"}')
    report = explain(crash, provider)

    assert report.diagnosis == crash.diagnosis  # the rules answer, unchanged
    assert report.diagnosis.source == "rules"
    assert any("invalid twice" in n for n in report.notes)


def test_provider_error_falls_back_to_rules(crash):
    provider = FakeProvider(ProviderError("Anthropic API rate limit reached (after retries)"))
    report = explain(crash, provider)

    assert report.diagnosis == crash.diagnosis
    assert report.notes == [
        "AI unavailable (Anthropic API rate limit reached (after retries)); answered from rules."
    ]
    assert len(provider.calls) == 1  # no repair call for a provider error


def test_code_fenced_json_is_accepted():
    answer = parse_answer(f"```json\n{llm_reply()}\n```")
    assert answer.failure_type is FailureType.CRASH_LOOP


def test_extra_fields_are_rejected():
    with pytest.raises(InvalidAnswer, match="schema"):
        parse_answer(llm_reply(severity="critical"))


# --- the model is held to the same standard as the rules ----------------------------------


def test_no_evidence_cited_means_low_confidence(crash):
    report = explain(crash, FakeProvider(llm_reply(evidence_used=[" "])))
    assert report.diagnosis.confidence == "low"
    assert report.diagnosis.evidence_used == []
    assert any("cited no evidence" in n for n in report.notes)


def test_disagreeing_with_a_high_certainty_rule_caps_confidence(crash):
    reply = llm_reply(failure_type="oom_killed", confidence="high")
    report = explain(crash, FakeProvider(reply))

    assert report.diagnosis.failure_type is FailureType.OOM_KILLED  # shown, not overridden
    assert report.diagnosis.confidence == "medium"
    assert any("classifier says crash_loop_backoff" in n for n in report.notes)


def test_disagreement_without_rule_backing_is_only_noted():
    unknown = replay_report("unknown")
    unknown = unknown.model_copy(update={"rule_hits": []})
    reply = llm_reply(failure_type="pending", confidence="medium")
    report = explain(unknown, FakeProvider(reply))
    assert report.diagnosis.confidence == "medium"
    assert any("classifier says unknown" in n for n in report.notes)


def test_unsure_ai_points_to_the_rule_that_found_a_cause(crash):
    report = explain(crash, FakeProvider(llm_reply(confidence="unsure")))
    assert report.diagnosis.confidence == "unsure"
    assert any("crash_missing_env_var" in n for n in report.notes)


def test_empty_fix_steps_are_dropped(crash):
    report = explain(crash, FakeProvider(llm_reply(fix=["Add DATABASE_URL", "", "  "])))
    assert report.diagnosis.fix == ["Add DATABASE_URL"]


# --- the schema sent to the provider -------------------------------------------------------


def test_schema_matches_the_answer_model():
    fields = set(LLMAnswer.model_fields)
    assert set(ANSWER_SCHEMA["properties"]) == fields
    assert set(ANSWER_SCHEMA["required"]) == fields
    assert ANSWER_SCHEMA["additionalProperties"] is False
    assert set(ANSWER_SCHEMA["properties"]["failure_type"]["enum"]) == {
        t.value for t in FailureType
    }


def test_schema_is_plain_json():
    assert json.loads(json.dumps(ANSWER_SCHEMA)) == ANSWER_SCHEMA
