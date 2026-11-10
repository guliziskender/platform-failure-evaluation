"""Redaction: the one bug this tool cannot ship is a leaked secret.

Fake secrets live in tests/fixtures/fake_secrets.py. Every leak must disappear; every benign
Kubernetes message must come through untouched, or the rules stop working.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pfa.ai.redact import redact_data, redact_evidence, redact_text, require_redacted
from pfa.cli import app
from pfa.engine.orchestrator import analyze, collect_evidence
from pfa.models import Evidence, OwnerRef, Target, WorkloadKind
from pfa.render import report_json

from .fixtures.fake_secrets import BENIGN, LEAKS, Leak

ROOT = Path(__file__).resolve().parents[1]
CRASH_FIXTURE = ROOT / "tests" / "fixtures" / "evidence" / "crash_loop_backoff.json"
REAL_EVIDENCE = sorted(
    [*ROOT.glob("tests/fixtures/evidence/*.json"), *ROOT.glob("lab/cases/*/evidence.json")]
)
ALL_LEAKS_LOG = "\n".join(leak.text for leak in LEAKS)


# --- text --------------------------------------------------------------------------------


@pytest.mark.parametrize("leak", LEAKS, ids=lambda leak: leak.id)
def test_secret_is_replaced_by_typed_placeholder(leak: Leak):
    redacted, counts = redact_text(leak.text)
    assert leak.secret not in redacted
    assert f"<REDACTED:{leak.kind}>" in redacted, redacted
    assert counts[leak.kind] >= 1


@pytest.mark.parametrize("leak", LEAKS, ids=lambda leak: leak.id)
def test_redaction_is_idempotent(leak: Leak):
    once, _ = redact_text(leak.text)
    twice, counts = redact_text(once)
    assert twice == once
    assert not counts


@pytest.mark.parametrize("text", BENIGN)
def test_kubernetes_messages_are_left_alone(text: str):
    assert redact_text(text) == (text, {})


def test_url_keeps_user_and_host():
    text, _ = redact_text("postgres://checkout:pfa-fake-db-pass@db.prod:5432/orders")
    assert text == "postgres://checkout:<REDACTED:password>@db.prod:5432/orders"


def test_quotes_and_neighbours_survive():
    text, _ = redact_text('{"user": "a", "password": "pfa-fake-json-pw", "port": 5432}')
    assert json.loads(text) == {"user": "a", "password": "<REDACTED:password>", "port": 5432}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Neighbouring pairs survive, however the value is delimited.
        (
            "Server=sql;User Id=sa;Password=pw1;Timeout=5",
            "Server=sql;User Id=sa;Password=<REDACTED:password>;Timeout=5",
        ),
        ("GET /cb?code=1&token=tok&x=2", "GET /cb?code=1&token=<REDACTED:token>&x=2"),
        ("user=a, password=pw2, port=5432", "user=a, password=<REDACTED:password>, port=5432"),
        ("password: pw3, user: bob", "password: <REDACTED:password>, user: bob"),
        # Escaped JSON inside a log line stays escaped JSON.
        (
            r'msg="{\"password\":\"pw4\"}" ts=1',
            r'msg="{\"password\":\"<REDACTED:password>\"}" ts=1',
        ),
        # A YAML block value: its lines go, the marker and the next key stay.
        (
            "client_secret: |\n    line1\n    line2\nport: 80",
            "client_secret: |\n    <REDACTED:secret>\nport: 80",
        ),
        # An unquoted value after ":" is the rest of the line, and only that line.
        ("password: a b c  \nnext", "password: <REDACTED:password>  \nnext"),
    ],
)
def test_exact_redaction(text, expected):
    assert redact_text(text)[0] == expected


def test_redacted_json_is_still_json():
    text, _ = redact_text(r'{"password": "a\"b' + "'" + r'c", "port": 1}')
    assert json.loads(text) == {"password": "<REDACTED:password>", "port": 1}


@pytest.mark.parametrize(
    "text",
    [
        "a-" * 50_000,  # was quadratic in the URL pattern: ~11 s per 100KB
        "a" * 100_000,
        "password=a&" * 10_000,
        "token: " * 15_000,
        "x://" * 25_000,
        'password="' + "\\" * 25_000,
    ],
    ids=["hyphens", "one_word", "ampersands", "colons", "schemes", "backslashes"],
)
def test_large_hostile_input_is_fast(text):
    # Container args and event messages have no size cap, so redaction must stay linear.
    started = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - started < 1.0


def test_every_leak_in_one_log_is_caught():
    redacted, counts = redact_text(ALL_LEAKS_LOG)
    assert [leak.id for leak in LEAKS if leak.secret in redacted] == []
    assert sum(counts.values()) >= len(LEAKS)


# --- structured data ---------------------------------------------------------------------


def test_data_redacts_nested_strings_and_keeps_names():
    summary = {
        "containers": [
            {
                "env_names": ["DB_USER", "DB_PASSWORD"],  # names only: not secrets
                "env_from": ["secret/app-secrets"],
                "args": ["--db-url", "postgres://u:pfa-fake-arg-pw@db/x", "--verbose"],
                "restart_count": 3,
            }
        ],
    }
    redacted, counts = redact_data(summary)
    c = redacted["containers"][0]
    assert c["env_names"] == ["DB_USER", "DB_PASSWORD"]
    assert c["env_from"] == ["secret/app-secrets"]
    assert c["args"] == ["--db-url", "postgres://u:<REDACTED:password>@db/x", "--verbose"]
    assert c["restart_count"] == 3
    assert counts == {"password": 1}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--password", "pfa-fake"], ["--password", "<REDACTED:password>"]),
        (["-api-token", "pfa-fake", "-v"], ["-api-token", "<REDACTED:token>", "-v"]),
        (["--client-secret", "pfa-fake"], ["--client-secret", "<REDACTED:secret>"]),
        # Flag followed by another flag, or by a reference: nothing to hide.
        (["--password", "--verbose"], ["--password", "--verbose"]),
        (["--password", "$(DB_PASSWORD)"], ["--password", "$(DB_PASSWORD)"]),
    ],
)
def test_secret_flag_value_in_next_list_item(args, expected):
    # Container args split flag and value into separate items; text patterns can't see that.
    assert redact_data(args)[0] == expected


def test_evidence_fields_are_all_redacted_and_marked():
    ev = Evidence(
        source="logs",
        summary={"note": "token=pfaFakeTok1"},
        raw_excerpt="password=pfaFakePw1",
        error="GET https://admin:pfaFakePw2@api failed",
    )
    out = redact_evidence(ev)
    text = out.model_dump_json()
    assert not any(s in text for s in ("pfaFakeTok1", "pfaFakePw1", "pfaFakePw2"))
    assert out.redacted is True
    assert out.redactions == {"password": 2, "token": 1}
    assert redact_evidence(out).redactions == out.redactions  # counted once


def test_require_redacted_guards_the_way_out():
    with pytest.raises(ValueError, match="logs"):
        require_redacted([Evidence(source="logs")])
    require_redacted([redact_evidence(Evidence(source="logs"))])


# --- no over-redaction on real evidence ---------------------------------------------------


@pytest.mark.parametrize("path", REAL_EVIDENCE, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_real_captured_evidence_is_unchanged(path: Path):
    # Real kind captures (lab snapshots may already hold placeholders). A false positive here
    # would mean a rule no longer sees the message it matches on.
    data = json.loads(path.read_text())
    assert redact_data(data) == (data, {})


# --- the pipeline: nothing unredacted gets past collection or analysis --------------------


def _crash_evidence_with_leaky_logs() -> tuple[Target, list[Evidence]]:
    data = json.loads(CRASH_FIXTURE.read_text())
    target = Target.model_validate(data["target"])
    evidence = [Evidence.model_validate(e) for e in data["evidence"]]
    logs = next(e for e in evidence if e.source == "logs")
    logs.raw_excerpt = f"{ALL_LEAKS_LOG}\n{logs.raw_excerpt}"
    return target, evidence


def test_analyze_redacts_replayed_evidence_and_still_diagnoses():
    target, evidence = _crash_evidence_with_leaky_logs()
    report = analyze(target, evidence)

    out = report_json(report, include_evidence=True)
    assert [leak.id for leak in LEAKS if leak.secret in out] == []
    assert all(e.redacted for e in report.evidence)
    # Redaction must not cost the diagnosis.
    assert report.rule_hits[0].rule == "crash_missing_env_var"
    assert report.diagnosis.confidence == "high"


class _LeakyCollector:
    name = "logs"

    def applies_to(self, failure):
        return True

    def collect(self, target):
        return Evidence(source=self.name, raw_excerpt=ALL_LEAKS_LOG)


class _LeakyError:
    name = "k8s_state"

    def applies_to(self, failure):
        return True

    def collect(self, target):
        raise RuntimeError("GET https://admin:pfa-fake-kubeconfig-pw@10.0.0.1:6443 timed out")


def test_collect_evidence_redacts_output_and_errors():
    target = Target(
        namespace="ns",
        name="app",
        kind=WorkloadKind.DEPLOYMENT,
        owners=[OwnerRef(kind="Deployment", name="app")],
    )
    evidence = collect_evidence(target, [_LeakyCollector(), _LeakyError()])
    out = json.dumps([e.model_dump(mode="json") for e in evidence])
    assert [leak.id for leak in LEAKS if leak.secret in out] == []
    assert "pfa-fake-kubeconfig-pw" not in out
    assert all(e.redacted for e in evidence)


@pytest.mark.parametrize(
    "extra", [[], ["--show-evidence"], ["-o", "json", "--show-evidence"]], ids=str
)
def test_cli_output_never_shows_a_secret(tmp_path, extra):
    target, evidence = _crash_evidence_with_leaky_logs()
    saved = tmp_path / "leaky.json"
    saved.write_text(
        json.dumps(
            {
                "target": target.model_dump(mode="json"),
                "evidence": [e.model_dump(mode="json") for e in evidence],
            }
        )
    )
    result = CliRunner().invoke(
        app, ["diagnose", "--from-evidence", str(saved), "--no-llm", *extra]
    )
    assert result.exit_code == 0, result.output
    assert [leak.id for leak in LEAKS if leak.secret in result.output] == []
