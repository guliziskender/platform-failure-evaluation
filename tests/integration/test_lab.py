"""`pfa collect` against the broken-pod lab. Run: make test-integration

Brings the lab up (idempotent; fast if it is already running) and checks, for every case in
lab/cases/, that the collected evidence contains what a diagnosis of that case needs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pfa.cli import app
from pfa.evaluation import missing_keywords

from .require import unavailable

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = "pfa-lab"
# Pod cases only: Docker build cases (build_log) are not deployed; tests/test_docker_build.py
# and tests/test_lab_cases.py cover them.
CASES = {
    p.parent.name: yaml.safe_load(p.read_text())
    for p in sorted((ROOT / "lab" / "cases").glob("*/expected.yaml"))
}
CASES = {k: v for k, v in CASES.items() if not v.get("build_log")}
FAKE_SECRET = "pfa-fake-secret-value"


@pytest.fixture(scope="module", autouse=True)
def lab():
    try:
        subprocess.run(["kubectl", "cluster-info"], check=True, capture_output=True, timeout=15)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        unavailable("no reachable Kubernetes cluster")
    result = subprocess.run(
        [sys.executable, str(ROOT / "lab" / "lab.py"), "up"], capture_output=True, text=True
    )
    if result.returncode != 0:
        pytest.fail(f"lab did not come up:\n{result.stdout}\n{result.stderr}")


def _collect(name: str, *extra: str) -> dict:
    result = CliRunner().invoke(app, ["collect", name, "-n", NAMESPACE, "-o", "json", *extra])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_every_failure_type_has_a_case():
    assert {c["failure_type"] for c in CASES.values()} == {
        "crash_loop_backoff",
        "oom_killed",
        "image_pull_backoff",
        "pending",
        "probe_failure",
    }


@pytest.mark.parametrize("case_id", CASES)
def test_collect_finds_the_evidence(case_id):
    expected = CASES[case_id]
    workload = expected["workload"]

    out = _collect(workload["name"])  # auto-detect the kind, as a user would

    assert out["target"]["kind"] == workload["kind"]
    assert out["target"]["pod"], "should have picked a pod"
    errors = {e["source"]: e["error"] for e in out["evidence"] if e["error"]}
    assert not errors, errors

    text = json.dumps(out)
    missing = [s for s in expected["evidence_must_contain"] if s not in text]
    assert not missing, f"evidence for {case_id} lacks {missing}"
    # Strings that must be in the collected logs: a container's spec often holds the same text
    # (its command echoes it), so searching all the evidence would not prove logs were read.
    logs = "".join(e.get("raw_excerpt") or "" for e in out["evidence"] if e["source"] == "logs")
    missing = [s for s in expected.get("logs_must_contain", []) if s not in logs]
    assert not missing, f"logs for {case_id} lack {missing}"
    assert FAKE_SECRET not in text


def test_unknown_name_exits_1():
    result = CliRunner().invoke(app, ["collect", "does-not-exist", "-n", NAMESPACE])
    assert result.exit_code == 1


# Not strict: a gap can depend on the cluster (a registry mirror changes the pull error).
RULES_CASES = [
    pytest.param(c, marks=pytest.mark.xfail(reason=e["known_gap"])) if e.get("known_gap") else c
    for c, e in CASES.items()
]


@pytest.mark.parametrize("case_id", RULES_CASES)
def test_diagnose_no_llm_matches_ground_truth(case_id):
    expected = CASES[case_id]
    result = CliRunner().invoke(
        app,
        ["diagnose", expected["workload"]["name"], "-n", NAMESPACE, "--no-llm", "-o", "json"],
    )
    assert result.exit_code == 0, result.output
    d = json.loads(result.stdout)["diagnosis"]

    assert d["failure_type"] == expected["failure_type"]
    assert d["confidence"] == expected.get("rules_confidence", "high"), d["likely_cause"]
    missing = missing_keywords(d["likely_cause"], expected["cause_keywords"])
    missing += missing_keywords(" ".join(d["fix"]), expected["fix_keywords"])
    assert not missing, f"{case_id}: diagnosis lacks {missing}: {d['likely_cause']}"


def test_live_eval_scores_every_case(tmp_path):
    result = CliRunner().invoke(
        app, ["eval", "--no-llm", "--live", "--results", str(tmp_path), "-o", "json"]
    )
    assert result.exit_code == 0, result.output
    run = json.loads(result.stdout)
    assert run["evidence"] == "live"
    assert run["errors"] == 0
    wrong = {r["case"] for r in run["results"] if not r["correct"] and not r["known_gap"]}
    assert not wrong, wrong
