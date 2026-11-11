"""Replay each lab case's saved evidence through classifier + rules; compare with expected.yaml.

Offline: pod cases use lab/cases/<id>/evidence.json (refresh with `make lab-snapshot`); Docker
build cases use their captured build.log (refresh with `python lab/lab.py build-logs`).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from pfa.evaluation import load_cases, missing_keywords, replay

CASES_DIR = Path(__file__).resolve().parents[1] / "lab" / "cases"
CASES = sorted(p.parent.name for p in CASES_DIR.glob("*/expected.yaml"))
BUILD_CASES = [c for c in CASES if (CASES_DIR / c / "Dockerfile").exists()]
POD_CASES = [c for c in CASES if c not in BUILD_CASES]
_EVAL_CASES = {c.id: c for c in load_cases(CASES_DIR)}


def _replay(case_id: str):
    return replay(CASES_DIR)(_EVAL_CASES[case_id])


def _expected(case_id: str) -> dict:
    return yaml.safe_load((CASES_DIR / case_id / "expected.yaml").read_text())


def test_every_lab_case_has_its_evidence():
    snapshots = sorted(p.parent.name for p in CASES_DIR.glob("*/evidence.json"))
    assert snapshots == POD_CASES, "run `make lab-snapshot` after adding a case"
    for case in BUILD_CASES:
        assert _expected(case)["build_log"] == "build.log", case
        assert (CASES_DIR / case / "build.log").read_text().strip(), case


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


def test_three_standard_cases_per_failure_type():
    # Hard cases come on top, so the standard set stays balanced and comparable over time.
    standard = [c for c in CASES if _expected(c).get("difficulty", "standard") == "standard"]
    counts = Counter(_expected(c)["failure_type"] for c in standard)
    assert set(counts.values()) == {3}, counts


def test_every_hard_case_says_what_the_trap_is():
    for c in CASES:
        if _expected(c).get("difficulty") == "hard":
            assert len(_expected(c).get("trap", "")) > 40, c


def test_every_case_is_deployed():
    kustomization = yaml.safe_load((CASES_DIR.parent / "kustomization.yaml").read_text())
    deployed = {Path(r).parent.name for r in kustomization["resources"]}
    assert set(POD_CASES) <= deployed
    assert not set(BUILD_CASES) & deployed  # a build case is a Dockerfile, not a manifest


@pytest.mark.parametrize("case_id", POD_CASES)
def test_snapshot_holds_no_secret_values(case_id):
    assert "pfa-fake-secret-value" not in (CASES_DIR / case_id / "evidence.json").read_text()
