"""Scoring, saved runs and `pfa eval`, on the real lab snapshots with fake AI answers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pfa import cli
from pfa.ai import providers
from pfa.ai.providers import ProviderError
from pfa.engine.orchestrator import analyze, load_evidence
from pfa.engine.target import TargetNotFound
from pfa.evaluation import (
    CaseError,
    EvalCase,
    EvalError,
    by_difficulty,
    by_failure_type,
    changes,
    live,
    load_cases,
    load_runs,
    missing_keywords,
    previous_run,
    replay,
    run_eval,
    save_run,
    score,
)
from pfa.models import FailureType

from .conftest import FakeProvider, llm_reply

CASES_DIR = Path(__file__).resolve().parents[1] / "lab" / "cases"
CRASH = "crashloop-missing-env"


def case(**overrides) -> EvalCase:
    fields = {
        "id": CRASH,
        "failure_type": FailureType.CRASH_LOOP,
        "cause_keywords": ["DATABASE_URL"],
        "fix_keywords": ["DATABASE_URL"],
    }
    return EvalCase(**(fields | overrides))


def report():
    return analyze(*load_evidence(CASES_DIR / CRASH / "evidence.json"))


@pytest.fixture
def one_miss(tmp_path) -> Path:
    """A copy of the standard lab cases where one answer key is deliberately wrong, so tests of
    misses don't depend on what the rules get wrong today (hard cases have real misses)."""
    cases = tmp_path / "cases"
    shutil.copytree(CASES_DIR, cases)
    for expected in cases.glob("*/expected.yaml"):
        if yaml.safe_load(expected.read_text()).get("difficulty") == "hard":
            shutil.rmtree(expected.parent)
    expected = cases / CRASH / "expected.yaml"
    text = expected.read_text().replace("cause_keywords: [DATABASE_URL]", "cause_keywords: [nope]")
    expected.write_text(text + "known_gap: deliberate, for the test\n")
    return cases


# --- keywords ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "keywords", "missing"),
    [
        ("Raise the Memory Limit", ["memory limit"], []),
        ("port 8080", ["80"], ["80"]),  # 8080 is not port 80
        ("port 180", ["80"], ["80"]),
        ("=80/TCP", ["80"], []),
        ("cpu requests", ["request"], []),
        ("a startup probe", [["startupProbe", "startup probe"]], []),
        ("nothing useful", ["cpu", ["a", "b"]], ["cpu", "a or b"]),
    ],
)
def test_missing_keywords(text, keywords, missing):
    assert missing_keywords(text, keywords) == missing


# --- scoring -------------------------------------------------------------------------------


def test_right_answer_scores_on_all_three():
    r = score(case(), report())
    assert (r.type_ok, r.cause_ok, r.fix_ok, r.correct, r.confident_wrong) == (
        True,
        True,
        True,
        True,
        False,
    )


@pytest.mark.parametrize(
    ("overrides", "failed"),
    [
        ({"failure_type": FailureType.PENDING}, "type_ok"),
        ({"cause_keywords": ["orders_idx"]}, "cause_ok"),
        ({"fix_keywords": ["orders_idx"]}, "fix_ok"),
    ],
)
def test_any_wrong_part_makes_the_answer_wrong(overrides, failed):
    r = score(case(**overrides), report())
    assert getattr(r, failed) is False
    assert r.correct is False
    assert r.confident_wrong is True  # the rules said "high" about this


def test_the_answer_is_kept_so_a_miss_can_be_read_later():
    r = score(case(cause_keywords=["nope"]), report())
    assert "DATABASE_URL" in r.cause
    assert r.fix and "DATABASE_URL" in r.fix[0]


def test_cause_keywords_must_be_in_the_cause_not_just_the_fix():
    rep = report()
    rep.diagnosis.likely_cause = "Something is wrong."
    r = score(case(), rep)
    assert (r.cause_ok, r.fix_ok) == (False, True)
    assert r.missing == ["cause: DATABASE_URL"]


def test_an_unsure_wrong_answer_is_not_confident_wrong():
    rep = report()
    rep.diagnosis.confidence = "unsure"
    assert score(case(cause_keywords=["orders_idx"]), rep).confident_wrong is False


def test_rules_fallback_is_not_credited_to_the_model():
    rep = report().model_copy(update={"notes": ["AI unavailable (timed out)"]})
    r = score(case(), rep, ai_expected=True)  # the rules' answer here is right
    assert r.correct is False
    assert r.confident_wrong is False
    assert r.error == "AI unavailable (timed out)"


# --- running --------------------------------------------------------------------------------


def test_rules_score_every_case_except_the_known_gaps():
    run = run_eval(load_cases(CASES_DIR), replay(CASES_DIR))
    wrong = {r.case for r in run.results if not r.correct}
    assert wrong == {r.case for r in run.results if r.known_gap}
    assert run.answered_by == "rules"
    assert run.correct == run.total - len(wrong)


def test_a_model_is_scored_on_its_own_answers():
    provider = FakeProvider(llm_reply())
    run = run_eval(load_cases(CASES_DIR, [CRASH]), replay(CASES_DIR), provider)
    assert run.answered_by == "fake-model"
    assert run.results[0].source == "llm"
    assert run.results[0].correct is True
    assert len(provider.calls) == 1


def test_provider_failures_count_as_errors():
    run = run_eval(
        load_cases(CASES_DIR, [CRASH]), replay(CASES_DIR), FakeProvider(ProviderError("down"))
    )
    assert (run.correct, run.errors) == (0, 1)
    assert "down" in run.results[0].error


def test_case_without_evidence_is_a_clean_error(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(EvalError, match="no usable evidence.json"):
        run_eval([case(id="x")], replay(tmp_path))


def test_bad_or_unknown_cases_are_clean_errors(tmp_path):
    with pytest.raises(EvalError, match="unknown case: nope"):
        load_cases(CASES_DIR, ["nope"])
    with pytest.raises(EvalError, match="no evaluation cases"):
        load_cases(tmp_path)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "expected.yaml").write_text("failure_type: pending\n")
    with pytest.raises(EvalError, match="cause_keywords"):
        load_cases(tmp_path)


def test_one_failed_case_does_not_stop_the_run():
    healthy = replay(CASES_DIR)

    def diagnose(c):
        if c.id == CRASH:
            raise CaseError("pod is gone")
        return healthy(c)

    cases = load_cases(CASES_DIR, [CRASH, "pending-cpu-units"])
    run = run_eval(cases, diagnose)
    gone = next(r for r in run.results if r.case == CRASH)
    assert (gone.correct, gone.error, gone.confident_wrong) == (False, "pod is gone", False)
    assert (run.correct, run.errors, run.total) == (1, 1, 2)


def test_live_run_looks_up_the_case_workload(monkeypatch):
    seen = []

    def not_found(clients, name, namespace, kind):
        seen.append((name, namespace, kind.value))
        raise TargetNotFound("no such workload")

    monkeypatch.setattr("pfa.evaluation.resolve_target", not_found)
    cases = load_cases(CASES_DIR, ["probe-wrong-port"])
    diagnose = live(None, None, "my-lab")
    run = run_eval(cases, diagnose, evidence="live")
    assert seen == [("inventory-api", "my-lab", "statefulset")]
    assert run.results[0].error == "no such workload"
    assert run.evidence == "live"


def test_live_case_needs_a_workload():
    run = run_eval([case()], live(None, None, "pfa-lab"), evidence="live")
    assert "names no workload" in run.results[0].error


def test_breakdown_by_failure_type(one_miss):
    types = by_failure_type(run_eval(load_cases(one_miss), replay(one_miss)))
    assert types["crash_loop_backoff"] == (2, 3)
    assert types["pending"] == (3, 3)
    assert sum(total for _, total in types.values()) == 15


# --- saved runs ------------------------------------------------------------------------------


def test_previous_run_only_compares_the_same_answerer(tmp_path):
    cases = load_cases(CASES_DIR, [CRASH])
    rules = run_eval(cases, replay(CASES_DIR))
    model = run_eval(cases, replay(CASES_DIR), FakeProvider(llm_reply()))
    assert previous_run(tmp_path, "rules") is None  # also: the folder may not exist yet

    save_run(rules, tmp_path)
    path = save_run(model, tmp_path)
    (tmp_path / "junk.json").write_text("not json")

    assert "fake-model" in path.name
    assert previous_run(tmp_path, "rules").answered_by == "rules"
    assert previous_run(tmp_path, "fake-model").results[0].source == "llm"
    assert previous_run(tmp_path, "other") is None


def test_runs_saved_in_the_same_second_are_both_kept(tmp_path):
    cases = load_cases(CASES_DIR, [CRASH])
    first = run_eval(cases, replay(CASES_DIR))
    second = run_eval(cases, replay(CASES_DIR), evidence="live")
    second.date = first.date.replace(microsecond=(first.date.microsecond + 1) % 1_000_000)
    assert save_run(first, tmp_path) != save_run(second, tmp_path)
    assert len(load_runs(tmp_path)) == 2


def test_changes_names_what_got_fixed_and_what_broke():
    now = run_eval(load_cases(CASES_DIR), replay(CASES_DIR))
    before = now.model_copy(deep=True)
    {r.case: r for r in before.results}[CRASH].correct = False  # was wrong, now right: fixed
    {r.case: r for r in now.results}["pending-cpu-units"].correct = False  # now wrong: broke
    assert changes(now, before) == ([CRASH], ["pending-cpu-units"])


def test_a_case_new_since_last_run_is_neither_fixed_nor_broken():
    now = run_eval(load_cases(CASES_DIR), replay(CASES_DIR))
    before = now.model_copy(update={"results": []})
    assert changes(now, before) == ([], [])


def test_previous_run_does_not_compare_replay_with_live(tmp_path):
    run = run_eval(load_cases(CASES_DIR, [CRASH]), replay(CASES_DIR))
    save_run(run, tmp_path)
    assert previous_run(tmp_path, "rules", "live") is None
    assert previous_run(tmp_path, "rules", "replay") is not None


def test_runs_from_before_live_mode_still_load(tmp_path):
    run = run_eval(load_cases(CASES_DIR, [CRASH]), replay(CASES_DIR))
    old = json.loads(run.model_dump_json())
    del old["evidence"]
    for field in ("seconds", "cause", "fix"):
        del old["results"][0][field]
    (tmp_path / "old.json").write_text(json.dumps(old))
    assert load_runs(tmp_path)[0].evidence == "replay"


# --- pfa eval --------------------------------------------------------------------------------


def invoke(tmp_path, *args: str, cases: Path = CASES_DIR):
    base = ["eval", "--cases", str(cases), "--results", str(tmp_path / "results")]
    return CliRunner().invoke(cli.app, [*base, *args])


def test_hard_cases_are_counted_apart_from_standard_ones(tmp_path):
    cases = tmp_path / "cases"
    shutil.copytree(CASES_DIR, cases)
    run = run_eval(load_cases(cases), replay(cases))
    levels = by_difficulty(run)
    assert list(levels) == ["standard", "hard"]
    assert levels["standard"][1] == 15
    assert sum(total for _, total in levels.values()) == run.total
    assert {r.difficulty for r in run.results if r.case == "crashloop-two-containers"} == {"hard"}


def test_breakdown_line_for_difficulty_only_when_there_are_hard_cases(tmp_path, one_miss):
    assert "standard" not in invoke(tmp_path, "--no-llm", "--no-save", cases=one_miss).stdout
    assert "standard 15/15 · hard " in invoke(tmp_path, "--no-llm", "--no-save").stdout


def test_eval_prints_saves_and_compares(tmp_path, one_miss):
    first = invoke(tmp_path, "--no-llm", cases=one_miss)
    assert first.exit_code == 0, first.output
    assert "Correct: 14/15" in first.stdout
    assert f"{CRASH}: missing cause: nope; known gap: deliberate" in first.stdout
    assert len(list((tmp_path / "results").glob("*-rules-replay.json"))) == 1
    assert "previous run" not in first.stdout

    assert "previous run" in invoke(tmp_path, "--no-llm", "--no-save", cases=one_miss).stdout
    assert len(list((tmp_path / "results").glob("*.json"))) == 1


def test_eval_json(tmp_path):
    run = json.loads(invoke(tmp_path, "--no-llm", "--no-save", "-o", "json").stdout)
    assert run["schema_version"] == 1
    assert run["total"] == len(list(CASES_DIR.glob("*/expected.yaml")))
    assert {r["difficulty"] for r in run["results"]} == {"standard", "hard"}


def test_partial_run_is_not_saved(tmp_path):
    result = invoke(tmp_path, "--no-llm", "--case", CRASH)
    assert "Correct: 1/1" in result.stdout
    assert not list((tmp_path / "results").glob("*"))


def test_unknown_case_is_a_usage_error(tmp_path):
    result = invoke(tmp_path, "--no-llm", "--case", "nope")
    assert result.exit_code == 2
    assert "unknown case" in result.output


def test_blocked_provider_stops_instead_of_scoring_the_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("PFA_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    result = invoke(tmp_path)
    assert result.exit_code == 2
    assert "send_data is off" in result.output
    assert not list((tmp_path / "results").glob("*"))


def test_no_llm_ignores_a_configured_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("PFA_PROVIDER", "anthropic")
    assert "pfa eval: rules" in invoke(tmp_path, "--no-llm", "--no-save").stdout


def test_a_send_is_announced_before_it_happens(tmp_path, monkeypatch):
    monkeypatch.setenv("PFA_PROVIDER", "anthropic")
    monkeypatch.setattr(providers, "make_provider", lambda s: FakeProvider(llm_reply()))
    result = invoke(tmp_path, "--case", CRASH)
    assert "about 1 case(s) (redacted evidence leaves this machine)" in result.stderr


def test_comparison_shows_what_changed(tmp_path):
    invoke(tmp_path, "--no-llm")
    path = next((tmp_path / "results").glob("*.json"))
    old = json.loads(path.read_text())
    for r in old["results"]:
        if r["case"] == "crashloop-missing-env":
            r["correct"] = False
    path.write_text(json.dumps(old))
    assert "Fixed since then: crashloop-missing-env" in invoke(tmp_path, "--no-llm").stdout


@pytest.mark.parametrize(("percent", "code"), [(93, 0), (94, 1), (100, 1)])
def test_fail_under(tmp_path, one_miss, percent, code):  # 14/15 is 93.3%
    args = ("--no-llm", "--no-save", "--fail-under", str(percent))
    assert invoke(tmp_path, *args, cases=one_miss).exit_code == code


def test_run_with_no_answers_is_a_failure_and_never_the_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("PFA_PROVIDER", "anthropic")
    down = FakeProvider(*[ProviderError("API down")] * len(list(CASES_DIR.glob("*/expected.yaml"))))
    monkeypatch.setattr(providers, "make_provider", lambda s: down)
    result = invoke(tmp_path)
    assert result.exit_code == 2
    assert "no case got an answer" in result.output
    assert not list((tmp_path / "results").glob("*"))


def test_some_failed_answers_are_still_saved(tmp_path, monkeypatch):
    def diagnose_or_fail(cases_dir):
        healthy = replay(cases_dir)

        def diagnose(c):
            if c.id == CRASH:
                raise CaseError("pod is gone")
            return healthy(c)

        return diagnose

    monkeypatch.setattr(cli, "replay", diagnose_or_fail)
    result = invoke(tmp_path, "--no-llm")
    assert result.exit_code == 0
    assert "No answer: 1" in result.stdout
    assert len(list((tmp_path / "results").glob("*.json"))) == 1


def test_live_flag_uses_the_cluster_and_is_recorded(tmp_path, monkeypatch):
    calls = []

    def fake_live(clients, settings, namespace):
        calls.append(namespace)
        return replay(CASES_DIR)

    monkeypatch.setattr(cli, "load_clients", lambda *a: "clients")
    monkeypatch.setattr(cli, "live", fake_live)
    result = invoke(tmp_path, "--no-llm", "--live", "-n", "other", "-o", "json")
    assert result.exit_code == 0, result.output
    assert calls == ["other"]
    assert json.loads(result.stdout)["evidence"] == "live"
    assert next((tmp_path / "results").glob("*.json")).exists()


def test_live_without_a_cluster_is_a_clean_error(tmp_path, monkeypatch):
    from pfa.kube import KubeConfigError

    def no_config(*args):
        raise KubeConfigError("no kubeconfig found")

    monkeypatch.setattr(cli, "load_clients", no_config)
    result = invoke(tmp_path, "--no-llm", "--live")
    assert result.exit_code == 2
    assert "no kubeconfig" in result.output


def test_history(tmp_path, one_miss):
    assert "No saved runs" in invoke(tmp_path, "--history").stdout
    invoke(tmp_path, "--no-llm", cases=one_miss)
    text = invoke(tmp_path, "--history").stdout
    assert "14/15" in text
    assert "replay" in text
    summary = json.loads(invoke(tmp_path, "--history", "-o", "json").stdout)
    assert summary[0]["correct"] == 14
    assert "results" not in summary[0]


def test_fail_on_new_misses_ignores_known_gaps_only(tmp_path, one_miss):
    # The real cases: every miss is a recorded known gap, so CI stays green.
    assert invoke(tmp_path, "--no-llm", "--no-save", "--fail-on-new-misses").exit_code == 0
    # Without its known_gap, the same miss is new and fails.
    expected = one_miss / CRASH / "expected.yaml"
    expected.write_text(expected.read_text().replace("known_gap: deliberate, for the test\n", ""))
    result = invoke(tmp_path, "--no-llm", "--no-save", "--fail-on-new-misses", cases=one_miss)
    assert result.exit_code == 1
    assert f"no known_gap: {CRASH}" in result.output
