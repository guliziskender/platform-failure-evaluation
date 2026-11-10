"""Classifier and rules on real captured evidence: one fixture per failure type.

Fixtures: tests/fixtures/evidence/<failure_type>.json (see the README there for provenance).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pfa.engine.orchestrator import analyze
from pfa.models import Evidence, FailureType, Report, Target

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"

# failure type -> (best rule, its certainty, text the cause must contain)
EXPECTED = {
    FailureType.CRASH_LOOP: ("crash_missing_env_var", "high", "DATABASE_URL"),
    FailureType.OOM_KILLED: ("oom_memory_limit", "high", "128Mi"),
    FailureType.IMAGE_PULL: ("image_not_found", "high", "1.36.1-uclbic"),
    FailureType.PENDING: ("pending_cpu_unit_typo", "high", "4000m"),
    FailureType.PROBE_FAILURE: ("probe_wrong_port", "high", "8080"),
    FailureType.UNKNOWN: ("workload_failed_create", "high", "ResourceQuota"),
}


def replay(failure_type: FailureType) -> Report:
    data = json.loads((FIXTURES / f"{failure_type.value}.json").read_text())
    target = Target.model_validate(data["target"])
    return analyze(target, [Evidence.model_validate(e) for e in data["evidence"]])


def test_there_is_a_fixture_for_every_failure_type():
    pod_types = set(FailureType) - {FailureType.DOCKER_BUILD}  # build logs come in a later task
    files = {p.stem for p in FIXTURES.glob("*.json")}
    assert files == {t.value for t in pod_types}


@pytest.mark.parametrize("failure_type", list(EXPECTED), ids=lambda t: t.value)
def test_classifier_recognises_failure_type(failure_type):
    assert replay(failure_type).classification.failure_type is failure_type


@pytest.mark.parametrize("failure_type", list(EXPECTED), ids=lambda t: t.value)
def test_best_rule_hit(failure_type):
    rule, certainty, must_mention = EXPECTED[failure_type]
    report = replay(failure_type)

    best = report.rule_hits[0]
    assert (best.rule, best.certainty) == (rule, certainty)
    assert best.failure_type is failure_type
    assert must_mention in best.cause

    d = report.diagnosis
    assert (d.failure_type, d.confidence) == (failure_type, certainty)
    assert d.rule_hits[0] == rule


def test_probe_fixture_was_captured_between_restarts():
    # The container is up and ready right now; only its last exit (137, killed by the kubelet)
    # and the probe events show the failure. (The CrashLoopBackOff-surface trap itself is
    # covered by test_classifier.py::test_probe_kill_is_not_mistaken_for_a_crash.)
    container = replay(FailureType.PROBE_FAILURE).evidence[0].summary["pod"]["containers"][0]
    assert "running" in container["state"] and container["ready"] is True
    last = container["last_state"]["terminated"]
    assert (last["exit_code"], last["reason"]) == (137, "Error")


def test_recency_uses_collection_time_not_wall_clock():
    # Without its probe events, the probe fixture is a container that was killed 1-2 minutes
    # before collection. Replayed days later it must still count as failing recently.
    report = replay(FailureType.PROBE_FAILURE)
    events = report.evidence[1].model_copy(update={"summary": {"events": []}})
    evidence = [report.evidence[0], events, *report.evidence[2:]]
    again = analyze(report.target, evidence)
    assert again.classification.failure_type is FailureType.CRASH_LOOP


def test_unknown_fixture_has_no_pod():
    report = replay(FailureType.UNKNOWN)
    assert report.target.pod is None
    assert "has no pods" in report.classification.problem


def test_healthy_fixture_reports_no_failure_and_no_rule_hits():
    report = replay(FailureType.NONE)
    assert report.classification.failure_type is FailureType.NONE
    assert report.rule_hits == []
    assert report.diagnosis.confidence == "high"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_fixtures_hold_no_secret_values(path):
    assert "pfa-fake-secret-value" not in path.read_text()
