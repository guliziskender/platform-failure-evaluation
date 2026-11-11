"""The prompt: what the model sees, in what order, within budget, and never a secret."""

from __future__ import annotations

import pytest

from pfa.ai.context import build_prompt
from pfa.ai.prompts import GUIDANCE
from pfa.models import Evidence, FailureType

from .conftest import EVIDENCE_FIXTURES, replay_report
from .fixtures.fake_secrets import LEAKS

TYPES = sorted(p.stem for p in EVIDENCE_FIXTURES.glob("*.json"))
SECTIONS = [
    "## Target",
    "## Classification (deterministic)",
    "## What to look at for this failure type",
    "## Pod and workload state",
    "## Events (Warnings first)",
    "## Container logs",
    "## Rule findings (deterministic hints)",
]


@pytest.mark.parametrize("failure_type", TYPES)
def test_sections_in_fixed_order(failure_type):
    user = build_prompt(replay_report(failure_type)).user
    positions = [user.index(s) for s in SECTIONS]
    assert positions == sorted(positions)
    assert user.rstrip().endswith("Reply with the JSON object only.")


def test_every_failure_type_has_guidance():
    assert set(GUIDANCE) == set(FailureType) - {FailureType.DOCKER_BUILD}


def test_crash_prompt_holds_what_a_diagnosis_needs():
    prompt = build_prompt(replay_report("crash_loop_backoff"))
    user = prompt.user
    assert "type: crash_loop_backoff" in user
    assert "DATABASE_URL is not set" in user  # log line
    assert "Back-off restarting failed container" in user  # event
    assert '"env_names":["DB_USER","DB_PASSWORD","LOG_LEVEL"]' in user
    assert "[high] crash_missing_env_var" in user  # rule hint
    assert GUIDANCE[FailureType.CRASH_LOOP] in user
    assert prompt.trimmed == []


def test_pending_prompt_says_logs_were_not_collected():
    # `pfa diagnose` skips the logs collector for Pending pods (the fixture, from `pfa collect`,
    # has it).
    report = replay_report("pending")
    report.evidence = [e for e in report.evidence if e.source != "logs"]
    user = build_prompt(report).user
    assert "not collected for this failure type" in user
    assert "Insufficient cpu" in user


def test_unknown_prompt_without_pod():
    user = build_prompt(replay_report("unknown")).user
    assert "no pod exists" in user
    assert "exceeded quota" in user


def test_long_logs_are_trimmed_from_the_head_to_fit():
    report = replay_report("crash_loop_backoff")
    logs = next(e for e in report.evidence if e.source == "logs")
    noise = "".join(f"startup noise line {i}\n" for i in range(3000))
    logs.raw_excerpt = noise + "FATAL: the real reason, at the end\n"

    prompt = build_prompt(report, budget_tokens=3000)
    assert prompt.est_tokens <= 3000
    assert "FATAL: the real reason, at the end" in prompt.user  # the tail survives
    assert "startup noise line 0\n" not in prompt.user
    assert prompt.trimmed and "log excerpt cut" in prompt.trimmed[0]


def test_many_events_are_trimmed_keeping_warnings_first():
    report = replay_report("crash_loop_backoff")
    events = next(e for e in report.evidence if e.source == "k8s_events")
    warning = {"type": "Warning", "reason": "BackOff", "message": "the important one"}
    filler = [{"type": "Normal", "reason": "Pulled", "message": "x" * 300}] * 60
    events.summary = {"events": [warning, *filler]}

    prompt = build_prompt(report, budget_tokens=2500)
    assert "the important one" in prompt.user
    assert any("events cut from 61" in t for t in prompt.trimmed)


def test_unredacted_evidence_is_refused():
    report = replay_report("crash_loop_backoff")
    raw = report.model_copy(update={"evidence": [Evidence(source="logs", raw_excerpt="hi")]})
    with pytest.raises(ValueError, match="not redacted"):
        build_prompt(raw)


def test_no_secret_reaches_the_prompt():
    # Leaks in logs, events, errors and args. analyze() redacts them; the prompt builder's
    # second pass would catch anything that slipped through.
    report = replay_report("crash_loop_backoff")
    leaky = "\n".join(leak.text for leak in LEAKS)
    for ev in report.evidence:
        ev.raw_excerpt = leaky
        ev.error = leaky
    report.evidence[0].summary["pod"]["containers"][0]["args"] = [leaky, "--password", "pw-1"]

    from pfa.engine.orchestrator import analyze

    prompt = build_prompt(analyze(report.target, report.evidence))
    text = prompt.system + prompt.user
    assert [leak.id for leak in LEAKS if leak.secret in text] == []
    assert "pw-1" not in text


def test_references_stay_readable_inside_json():
    # Inside JSON a `"` after `$DB_PASSWORD` is escaped as `\"`. Redacting the encoded text
    # misread that as a secret value; the prompt must keep the reference.
    report = replay_report("crash_loop_backoff")
    container = report.evidence[0].summary["pod"]["containers"][0]
    container["args"] = ['echo "password=$DB_PASSWORD"']
    assert "password=$DB_PASSWORD" in build_prompt(report).user


def test_prompt_names_every_failing_container_and_its_findings():
    # qwen3:8b missed the second container while its finding was left out of the hints.
    from pathlib import Path

    from pfa.engine.orchestrator import analyze, load_evidence

    case = Path(__file__).parents[1] / "lab/cases/crashloop-two-containers/evidence.json"
    report = analyze(*load_evidence(case))
    user = build_prompt(report).user
    assert "container api. Also failing: worker (diagnose each)" in user
    assert "crash_missing_file: Container worker: The app exits because" in user
    assert "=== worker: previous container ===" in user
