"""Replay each lab case's saved evidence through classifier + rules; compare with expected.yaml.

Offline: uses lab/cases/<id>/evidence.json (refresh with `make lab-snapshot`).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from pfa.engine.orchestrator import analyze, load_evidence
from pfa.evaluation import missing_keywords

CASES_DIR = Path(__file__).resolve().parents[1] / "lab" / "cases"
CASES = sorted(p.parent.name for p in CASES_DIR.glob("*/evidence.json"))


def _replay(case_id: str):
    return analyze(*load_evidence(CASES_DIR / case_id / "evidence.json"))


def _expected(case_id: str) -> dict:
    return yaml.safe_load((CASES_DIR / case_id / "expected.yaml").read_text())


def test_every_lab_case_has_a_snapshot():
    all_cases = sorted(p.parent.name for p in CASES_DIR.glob("*/expected.yaml"))
    assert CASES == all_cases, "run `make lab-snapshot` after adding a case"


def _rules_cases() -> list:
    """Every case; strict, so fixing a known gap fails here until `known_gap` is removed."""
    return [
        pytest.param(c, marks=pytest.mark.xfail(strict=True, reason=gap))
        if (gap := _expected(c).get("known_gap"))
        else c
        for c in CASES
    ]


@pytest.mark.parametrize("case_id", _rules_cases())
def test_rules_diagnose_lab_case(case_id):
    expected = _expected(case_id)
    report = _replay(case_id)
    d = report.diagnosis

    assert report.classification.failure_type.value == expected["failure_type"]
    assert d.failure_type.value == expected["failure_type"]
    assert d.confidence == expected.get("rules_confidence", "high"), d.likely_cause

    missing = missing_keywords(d.likely_cause, expected["cause_keywords"])
    missing += missing_keywords(" ".join(d.fix), expected["fix_keywords"])
    assert not missing, f"diagnosis lacks {missing}: {d.likely_cause}"


def test_three_cases_per_failure_type():
    counts = Counter(_expected(c)["failure_type"] for c in CASES)
    assert set(counts.values()) == {3}, counts


def test_every_case_is_deployed():
    kustomization = yaml.safe_load((CASES_DIR.parent / "kustomization.yaml").read_text())
    assert set(CASES) <= {Path(r).parent.name for r in kustomization["resources"]}


@pytest.mark.parametrize("case_id", CASES)
def test_snapshot_holds_no_secret_values(case_id):
    assert "pfa-fake-secret-value" not in (CASES_DIR / case_id / "evidence.json").read_text()
