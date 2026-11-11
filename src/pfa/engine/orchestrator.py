"""Runs one diagnosis end to end: collect, redact, classify, apply rules, ask the AI.

No Kubernetes- or LLM-specific code here: collectors and the AI provider plug in.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pfa.ai.llm import explain
from pfa.ai.providers.base import LLMProvider
from pfa.ai.redact import redact_all, redact_evidence
from pfa.ai.rules import answer_from_rules, run_rules
from pfa.collectors.base import Collector, run_safely
from pfa.collectors.k8s_events import K8sEventsCollector
from pfa.collectors.k8s_state import K8sStateCollector
from pfa.collectors.logs import LogsCollector
from pfa.config import Settings
from pfa.engine.classifier import classify
from pfa.kube import KubeClients
from pfa.models import Evidence, FailureType, Report, Target
from pfa.view import EvidenceView

# Collected first, because classification needs them; the rest depend on the failure type.
CORE_SOURCES = ("k8s_state", "k8s_events")


def default_collectors(clients: KubeClients, settings: Settings) -> list[Collector]:
    return [
        K8sStateCollector(clients),
        K8sEventsCollector(clients, window_minutes=settings.event_window_minutes),
        LogsCollector(
            clients, tail_lines=settings.log_tail_lines, max_bytes=settings.log_max_bytes
        ),
    ]


def collect_evidence(
    target: Target,
    collectors: list[Collector],
    failure: FailureType = FailureType.UNKNOWN,
) -> list[Evidence]:
    """Run every applicable collector in parallel, redacting each result as it comes in.

    Output order matches `collectors`. Nothing unredacted leaves this function.
    """
    selected = [c for c in collectors if c.applies_to(failure)]
    if not selected:
        return []
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        return list(pool.map(lambda c: redact_evidence(run_safely(c, target)), selected))


def run_diagnosis(
    target: Target,
    collectors: list[Collector],
    provider: LLMProvider | None = None,
    budget_tokens: int = 6000,
) -> Report:
    """Collect in two phases (core evidence, classify, then type-specific), then answer."""
    core = [c for c in collectors if c.name in CORE_SOURCES]
    rest = [c for c in collectors if c.name not in CORE_SOURCES]
    evidence = collect_evidence(target, core)
    failure = classify(EvidenceView(target, evidence)).failure_type
    evidence += collect_evidence(target, rest, failure)
    return answer(analyze(target, evidence), provider, budget_tokens)


def answer(report: Report, provider: LLMProvider | None, budget_tokens: int = 6000) -> Report:
    """The rules answer, or the AI's when a provider is given (falls back to rules on failure)."""
    return explain(report, provider, budget_tokens) if provider else report


def load_evidence(path: Path) -> tuple[Target, list[Evidence]]:
    """Read a saved `pfa collect -o json` file, for replaying without a cluster."""
    data = json.loads(path.read_text())
    return Target.model_validate(data["target"]), [
        Evidence.model_validate(e) for e in data["evidence"]
    ]


def analyze(target: Target, evidence: list[Evidence]) -> Report:
    """Classify and apply rules to evidence. Pure: works on live or replayed evidence.

    Redacts again first: replayed evidence files may be hand-edited or predate redaction.
    """
    evidence = redact_all(evidence)
    view = EvidenceView(target, evidence)
    classification = classify(view)
    hits = run_rules(view, classification)
    others = []  # (container label, its best hit) for each other failing container
    for name in target.also_failing:
        other = EvidenceView(target.model_copy(update={"container": name}), evidence)
        other_hits = run_rules(other, classification)
        if other_hits and other_hits[0].failure_type is classification.failure_type:
            others.append((other.container_label, other_hits[0]))
    # The other containers' findings are hints for the AI too, named by container: given only
    # the first container's, a model trusts them and misses the second failure.
    hints = hits + [
        h.model_copy(update={"finding": f"{label[:1].upper()}{label[1:]}: {h.finding}"})
        for label, h in others
        if h.cause not in {x.cause for x in hits}
    ]
    return Report(
        target=target,
        classification=classification,
        rule_hits=hints,
        diagnosis=answer_from_rules(classification, hits, view.container_label, others),
        evidence=evidence,
    )
