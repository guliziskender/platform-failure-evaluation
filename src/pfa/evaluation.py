"""Score diagnoses against labelled lab cases, so accuracy is a number and not an impression.

Each case's evidence comes from its saved snapshot (`lab/cases/<id>/evidence.json`) or, with
`live`, from the running lab. It is diagnosed by the rules or a model and compared with
`expected.yaml`. Saved runs make a prompt or rule change show up as that number going up or down.
Only reads the cluster: deploying the lab is `lab/lab.py`'s job.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, Field, ValidationError
from urllib3.exceptions import HTTPError

from pfa.ai.providers.base import LLMProvider
from pfa.config import Settings
from pfa.engine.orchestrator import (
    analyze,
    answer,
    default_collectors,
    load_evidence,
    run_diagnosis,
)
from pfa.engine.target import TargetNotFound, resolve_target
from pfa.kube import KubeClients
from pfa.models import Confidence, FailureType, Report, WorkloadKind

EVAL_SCHEMA_VERSION = 1
# A wrong answer at these levels is the expensive kind: people act on it.
_CONFIDENT: set[Confidence] = {"high", "medium"}

EvidenceMode = Literal["replay", "live"]

# One entry per fact the answer must state. A list entry accepts any of its alternatives.
Keywords = list[str | list[str]]
# standard: one clear failure. hard: a trap (misleading logs or events, two failures at once,
# a cause outside the pod). Reported separately, so hard cases never hide a standard regression.
Difficulty = Literal["standard", "hard"]


class EvalError(Exception):
    """The cases cannot be loaded."""


class CaseError(Exception):
    """This one case has no report (say, its pod is gone). The run goes on without it."""


class Workload(BaseModel):
    kind: WorkloadKind
    name: str


class EvalCase(BaseModel):
    id: str
    workload: Workload | None = None  # which app to diagnose in a live run
    failure_type: FailureType
    cause_keywords: Keywords
    fix_keywords: Keywords
    known_gap: str | None = None  # why the rules are expected to miss this case
    difficulty: Difficulty = "standard"


class CaseResult(BaseModel):
    case: str
    expected: FailureType
    answered: FailureType
    confidence: Confidence
    source: str
    type_ok: bool
    cause_ok: bool
    fix_ok: bool
    correct: bool
    confident_wrong: bool
    missing: list[str] = Field(default_factory=list)  # expected facts the answer did not state
    error: str | None = None  # no answer to score: the AI or the live collection failed
    known_gap: str | None = None
    difficulty: Difficulty = "standard"
    seconds: float = 0.0
    # What was answered, so a miss can be read (and a too-strict answer key spotted) later.
    cause: str = ""
    fix: list[str] = Field(default_factory=list)


class EvalRun(BaseModel):
    schema_version: int = EVAL_SCHEMA_VERSION
    date: datetime
    answered_by: str  # "rules" or the model name
    evidence: EvidenceMode = "replay"
    total: int
    correct: int
    type_correct: int
    cause_correct: int
    fix_correct: int
    confident_wrong: int
    errors: int
    results: list[CaseResult]


def missing_keywords(text: str, keywords: Keywords) -> list[str]:
    """The entries of `keywords` not found in `text`. Case-insensitive. A match starts at a word
    boundary and cannot run into more digits: "80" is not in "8080" or "180", but "request" is
    in "requests"."""
    missing = []
    for entry in keywords:
        options = [entry] if isinstance(entry, str) else entry
        if not any(_found(option, text) for option in options):
            missing.append(" or ".join(options))
    return missing


def _found(keyword: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\d)", text, re.I) is not None


def load_cases(cases_dir: Path, only: list[str] | None = None) -> list[EvalCase]:
    cases = []
    for path in sorted(cases_dir.glob("*/expected.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
            cases.append(EvalCase(id=path.parent.name, **data))
        except (yaml.YAMLError, TypeError, ValidationError) as exc:
            raise EvalError(f"{path}: {exc}") from exc
    if not cases:
        raise EvalError(f"no evaluation cases (*/expected.yaml) in {cases_dir}")
    if only:
        unknown = sorted(set(only) - {c.id for c in cases})
        if unknown:
            raise EvalError(f"unknown case: {', '.join(unknown)}")
        cases = [c for c in cases if c.id in only]
    return cases


def replay(cases_dir: Path) -> Callable[[EvalCase], Report]:
    """Diagnose from each case's saved evidence: repeatable, and identical for every model."""

    def diagnose(case: EvalCase) -> Report:
        try:
            return analyze(*load_evidence(cases_dir / case.id / "evidence.json"))
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise EvalError(f"{case.id}: no usable evidence.json ({exc})") from exc

    return diagnose


def live(clients: KubeClients, settings: Settings, namespace: str) -> Callable[[EvalCase], Report]:
    """Diagnose from the running lab: also exercises target selection and the collectors."""

    def diagnose(case: EvalCase) -> Report:
        if case.workload is None:
            raise CaseError("expected.yaml names no workload")
        try:
            target = resolve_target(clients, case.workload.name, namespace, case.workload.kind)
        except (TargetNotFound, ApiException, HTTPError) as exc:
            raise CaseError(str(exc)) from exc
        return run_diagnosis(target, default_collectors(clients, settings))

    return diagnose


def score(case: EvalCase, report: Report, ai_expected: bool = False) -> CaseResult:
    """Compare one answer with the case. If the AI was asked but the answer came from the rules
    fallback, it is an error: crediting the rules' answer to the model would inflate its score."""
    d = report.diagnosis
    error = None
    if ai_expected and d.source != "llm":
        error = report.notes[-1] if report.notes else "the AI gave no answer"
    missing_cause = missing_keywords(d.likely_cause, case.cause_keywords)
    missing_fix = missing_keywords(" ".join(d.fix), case.fix_keywords)
    type_ok = d.failure_type is case.failure_type and not error
    cause_ok = not missing_cause and not error
    fix_ok = not missing_fix and not error
    correct = type_ok and cause_ok and fix_ok
    return CaseResult(
        case=case.id,
        expected=case.failure_type,
        answered=d.failure_type,
        confidence=d.confidence,
        source=d.source,
        type_ok=type_ok,
        cause_ok=cause_ok,
        fix_ok=fix_ok,
        correct=correct,
        confident_wrong=not correct and not error and d.confidence in _CONFIDENT,
        missing=[f"cause: {m}" for m in missing_cause] + [f"fix: {m}" for m in missing_fix],
        error=error,
        known_gap=case.known_gap,
        difficulty=case.difficulty,
        cause=d.likely_cause,
        fix=d.fix,
    )


def _no_report(case: EvalCase, message: str) -> CaseResult:
    return CaseResult(
        case=case.id,
        expected=case.failure_type,
        answered=FailureType.UNKNOWN,
        confidence="unsure",
        source="none",
        type_ok=False,
        cause_ok=False,
        fix_ok=False,
        correct=False,
        confident_wrong=False,
        error=message,
        known_gap=case.known_gap,
        difficulty=case.difficulty,
    )


def run_eval(
    cases: list[EvalCase],
    diagnose: Callable[[EvalCase], Report],
    provider: LLMProvider | None = None,
    budget_tokens: int = 6000,
    on_case: Callable[[EvalCase], None] = lambda case: None,
    evidence: EvidenceMode = "replay",
) -> EvalRun:
    results = []
    for case in cases:
        on_case(case)
        started = time.monotonic()
        try:
            report = answer(diagnose(case), provider, budget_tokens)
            result = score(case, report, ai_expected=provider is not None)
        except CaseError as exc:
            result = _no_report(case, str(exc))
        result.seconds = round(time.monotonic() - started, 2)
        results.append(result)
    return EvalRun(
        date=datetime.now(UTC),
        answered_by=provider.model if provider else "rules",
        evidence=evidence,
        total=len(results),
        correct=sum(r.correct for r in results),
        type_correct=sum(r.type_ok for r in results),
        cause_correct=sum(r.cause_ok for r in results),
        fix_correct=sum(r.fix_ok for r in results),
        confident_wrong=sum(r.confident_wrong for r in results),
        errors=sum(r.error is not None for r in results),
        results=results,
    )


def by_failure_type(run: EvalRun) -> dict[str, tuple[int, int]]:
    """{failure type: (correct, total)}, by what the case really is."""
    totals = Counter(r.expected.value for r in run.results)
    correct = Counter(r.expected.value for r in run.results if r.correct)
    return {name: (correct[name], totals[name]) for name in sorted(totals)}


def by_difficulty(run: EvalRun) -> dict[str, tuple[int, int]]:
    """{difficulty: (correct, total)}, standard first."""
    totals = Counter(r.difficulty for r in run.results)
    correct = Counter(r.difficulty for r in run.results if r.correct)
    return {name: (correct[name], totals[name]) for name in ("standard", "hard") if totals[name]}


def changes(run: EvalRun, previous: EvalRun) -> tuple[list[str], list[str]]:
    """(cases that got fixed, cases that broke) compared with the previous run."""
    before = {r.case: r.correct for r in previous.results}
    fixed = [r.case for r in run.results if r.correct and before.get(r.case) is False]
    broken = [r.case for r in run.results if not r.correct and before.get(r.case) is True]
    return fixed, broken


def save_run(run: EvalRun, results_dir: Path) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    who = re.sub(r"[^\w.-]+", "-", run.answered_by)
    # Microseconds, because two runs in one second must not overwrite each other.
    path = results_dir / f"{run.date:%Y%m%d-%H%M%S-%f}-{who}-{run.evidence}.json"
    path.write_text(run.model_dump_json(indent=2) + "\n")
    return path


def load_runs(results_dir: Path) -> list[EvalRun]:
    """Every saved run, oldest first."""
    runs = []
    for path in results_dir.glob("*.json"):
        try:
            runs.append(EvalRun.model_validate(json.loads(path.read_text())))
        except (ValueError, ValidationError):
            continue  # an old or hand-edited file must not stop the evaluation
    return sorted(runs, key=lambda r: r.date)


def previous_run(
    results_dir: Path, answered_by: str, evidence: EvidenceMode = "replay"
) -> EvalRun | None:
    """The latest run by the same answerer on the same kind of evidence: the only fair baseline."""
    same = [
        r for r in load_runs(results_dir) if r.answered_by == answered_by and r.evidence == evidence
    ]
    return same[-1] if same else None
