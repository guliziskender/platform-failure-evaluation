"""Builds the prompt: a compact, fixed-order view of the evidence, within a token budget.

This is the last stop before data leaves the process, so it refuses unredacted evidence and
redacts every piece once more as it is rendered: structures before they are JSON-encoded, text
as text. (Not one pass over the finished prompt: inside JSON, `"` becomes `\"`, which the
patterns would misread.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pfa.ai.prompts import SYSTEM, guidance_for
from pfa.ai.redact import redact_data, redact_text, require_redacted
from pfa.models import Evidence, Report

CHARS_PER_TOKEN = 4  # rough, but stable and dependency-free
MIN_EVENTS = 5
_HANDLED = ("k8s_state", "k8s_events", "logs")


@dataclass
class Prompt:
    system: str
    user: str
    trimmed: list[str] = field(default_factory=list)  # what was cut to fit the budget

    @property
    def est_tokens(self) -> int:
        return estimate_tokens(self.system + self.user)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def build_prompt(report: Report, budget_tokens: int = 6000) -> Prompt:
    require_redacted(report.evidence)
    by_source = {e.source: e for e in report.evidence}
    logs = by_source.get("logs")
    log_text = (logs.raw_excerpt or "") if logs else ""
    events = list(_summary(by_source.get("k8s_events")).get("events", []))

    trimmed: list[str] = []
    user = _render(report, by_source, log_text, events)
    over = estimate_tokens(SYSTEM + user) - budget_tokens

    # Logs go first: their tail holds the crash, and the head is usually startup noise.
    if over > 0 and log_text:
        keep = max(0, len(log_text) - over * CHARS_PER_TOKEN)
        cut = log_text[len(log_text) - keep :]
        cut = cut[cut.find("\n") + 1 :] if "\n" in cut else cut  # start on a whole line
        trimmed.append(f"log excerpt cut from {len(log_text)} to {len(cut)} characters")
        log_text = cut
        user = _render(report, by_source, log_text, events)
        over = estimate_tokens(SYSTEM + user) - budget_tokens

    # Then events: Warnings are sorted first by the collector, so keep the head.
    while over > 0 and len(events) > MIN_EVENTS:
        events = events[: max(MIN_EVENTS, len(events) // 2)]
        user = _render(report, by_source, log_text, events)
        over = estimate_tokens(SYSTEM + user) - budget_tokens
    original_events = len(_summary(by_source.get("k8s_events")).get("events", []))
    if len(events) < original_events:
        trimmed.append(f"events cut from {original_events} to {len(events)}")

    return Prompt(system=SYSTEM, user=user, trimmed=trimmed)


def _render(
    report: Report, by_source: dict[str, Evidence], log_text: str, events: list[Any]
) -> str:
    t, c = report.target, report.classification
    parts: list[str] = []

    target = f"{t.kind.value}/{t.name} in namespace {t.namespace}"
    target += f", pod {t.pod}" if t.pod else ", no pod exists"
    if t.container:
        target += f", container {t.container}"
    parts.append(_section("Target", _text(target)))

    classification = [f"type: {c.failure_type.value}", f"problem: {c.problem}"]
    classification += [f"signal: {s}" for s in c.signals]
    parts.append(_section("Classification (deterministic)", _text("\n".join(classification))))
    parts.append(_section("What to look at for this failure type", guidance_for(c.failure_type)))

    state = by_source.get("k8s_state")
    parts.append(_section("Pod and workload state", _json(_summary(state)) or "none collected"))
    parts.append(_section("Events (Warnings first)", _json(events) if events else "none"))

    logs = by_source.get("logs")
    if logs is not None:
        info = _json({k: v for k, v in _summary(logs).items() if k != "container"})
        parts.append(_section("Container logs", f"{info}\n{_text(log_text) or '(no log lines)'}"))
    else:
        parts.append(_section("Container logs", "not collected for this failure type"))

    for ev in report.evidence:
        if ev.source not in _HANDLED and (ev.summary or ev.raw_excerpt):
            parts.append(_section(f"Evidence: {ev.source}", _json(ev.summary)))

    errors = [f"{e.source}: {e.error}" for e in report.evidence if e.error]
    if errors:
        parts.append(_section("Evidence that could not be collected", _text("\n".join(errors))))

    if report.rule_hits:
        hits = [
            f"- [{h.certainty}] {h.rule}: {h.finding}\n  cause: {h.cause}\n"
            f"  fix: {'; '.join(h.fix)}"
            for h in report.rule_hits
        ]
        parts.append(_section("Rule findings (deterministic hints)", _text("\n".join(hits))))
    else:
        parts.append(_section("Rule findings (deterministic hints)", "no rule matched"))

    parts.append("Diagnose this failure. Reply with the JSON object only.")
    return "\n\n".join(parts)


def _section(title: str, body: str) -> str:
    return f"## {title}\n{body}"


def _summary(ev: Evidence | None) -> dict[str, Any]:
    return ev.summary if ev else {}


# Second line of defense: evidence is redacted already (require_redacted), so these find nothing
# unless something upstream changed.
def _text(text: str) -> str:
    return redact_text(text)[0]


def _json(data: Any) -> str:
    if not data:
        return ""
    return json.dumps(redact_data(data)[0], separators=(",", ":"), ensure_ascii=False)
