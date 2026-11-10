"""AI diagnosis of every lab case with a real model. Run: make test-llm

Needs the lab (brought up by test_lab.py's fixture logic) and a configured provider, e.g.
PFA_PROVIDER=anthropic PFA_SEND_DATA=1 ANTHROPIC_API_KEY=... or PFA_PROVIDER=ollama.
Skipped otherwise. Only fake lab data is sent. `pfa eval` gives the scored version of this.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pfa.ai.providers import ProviderConfigError, make_provider
from pfa.cli import app
from pfa.config import Settings
from pfa.evaluation import missing_keywords

from .test_lab import CASES, NAMESPACE, lab  # noqa: F401  (lab: module fixture, brings it up)

pytestmark = [pytest.mark.integration, pytest.mark.llm]


@pytest.fixture(scope="module", autouse=True)
def provider_configured():
    try:
        if make_provider(Settings.load()) is None:
            pytest.skip("no AI provider configured (PFA_PROVIDER)")
    except ProviderConfigError as exc:
        pytest.skip(str(exc))


@pytest.mark.parametrize("case_id", CASES)
def test_ai_diagnosis_matches_ground_truth(case_id):
    expected = CASES[case_id]
    result = CliRunner().invoke(
        app, ["diagnose", expected["workload"]["name"], "-n", NAMESPACE, "-o", "json"]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    d = report["diagnosis"]

    assert d["source"] == "llm", report["notes"]
    assert d["failure_type"] == expected["failure_type"]
    assert d["confidence"] in ("high", "medium"), d["likely_cause"]
    assert d["evidence_used"]
    missing = missing_keywords(
        d["likely_cause"] + " " + " ".join(d["fix"]), expected["cause_keywords"]
    )
    assert not missing, f"{case_id}: AI answer lacks {missing}: {d['likely_cause']}"
    assert "pfa-fake-secret-value" not in result.stdout
