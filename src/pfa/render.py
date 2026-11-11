"""Terminal and JSON output."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console, Group
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pfa.ai.context import Prompt
from pfa.config import OPTIONS, SECRET_OPTIONS, Settings
from pfa.evaluation import CaseResult, EvalRun, by_difficulty, by_failure_type, changes
from pfa.models import Diagnosis, Evidence, Report, Target, WorkloadKind

_CONFIDENCE_STYLE = {"high": "green", "medium": "yellow", "low": "red", "unsure": "red"}


def evidence_json(target: Target, evidence: list[Evidence]) -> str:
    return json.dumps(
        {
            "target": target.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in evidence],
        },
        indent=2,
    )


def print_evidence(
    console: Console, target: Target, evidence: list[Evidence], header: bool = True
) -> None:
    if header:
        console.print(_target_line(target))
    for ev in evidence:
        parts = []
        if ev.error:
            parts.append(Text(f"collector failed: {ev.error}", style="bold red"))
        if ev.summary:
            parts.append(JSON.from_data(ev.summary))
        if ev.raw_excerpt:
            parts.append(Panel(Text(ev.raw_excerpt), title="excerpt", border_style="dim"))
        style = "red" if ev.error else "cyan"
        console.print(
            Panel(
                Group(*parts),
                title=ev.source,
                title_align="left",
                subtitle=_redaction_note(ev),
                subtitle_align="right",
                border_style=style,
            )
        )


def _redaction_note(ev: Evidence) -> Text | None:
    if not ev.redactions:
        return None
    kinds = ", ".join(f"{n} {kind}" for kind, n in ev.redactions.items())
    return Text(f"redacted: {kinds}", style="yellow")


def _target_line(target: Target) -> Text:
    line = Text()
    if target.kind is WorkloadKind.BUILD:
        line.append(f"docker build log {target.name}", style="bold")
        return line
    line.append(f"{target.kind.value}/{target.name}", style="bold")
    line.append(f"  namespace={target.namespace}")
    if target.pod:
        line.append(f"  pod={target.pod}")
    else:
        line.append("  pod=<none>", style="yellow")
    if target.container:
        line.append(f"  container={target.container}")
    if target.owners:
        chain = " -> ".join(f"{o.kind}/{o.name}" for o in target.owners)
        line.append(f"\nowners: {chain}", style="dim")
    return line


def report_json(report: Report, include_evidence: bool = False) -> str:
    exclude = None if include_evidence else {"evidence"}
    return report.model_dump_json(indent=2, exclude=exclude)


def print_diagnosis(console: Console, report: Report, show_evidence: bool = False) -> None:
    d = report.diagnosis
    console.print(_target_line(report.target))
    console.print()

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()
    # Text(), not str: cluster messages contain "[...]", which Rich would parse as markup.
    grid.add_row("Failure type", Text(d.failure_type.value, style="bold"))
    grid.add_row("Problem", Text(d.problem))
    grid.add_row("Likely cause", Text(d.likely_cause))
    if d.fix:
        grid.add_row("Fix", Text("\n".join(f"{i}. {step}" for i, step in enumerate(d.fix, 1))))
    confidence = Text(d.confidence.capitalize(), style=_CONFIDENCE_STYLE[d.confidence])
    confidence.append(f" · {_answered_by(d)}", style="dim")
    grid.add_row("Confidence", confidence)
    if d.evidence_used:
        grid.add_row("Evidence", Text("\n".join(f"• {e}" for e in d.evidence_used), style="dim"))
    console.print(grid)

    if report.notes:
        console.print()
        for note in report.notes:
            console.print(Text(f"note: {note}", style="yellow"))

    console.print()
    console.print(Text("Rule findings", style="bold"))
    if not report.rule_hits:
        console.print(Text("  no rule matched", style="dim"))
    findings = Table.grid(padding=(0, 2))
    findings.add_column(no_wrap=True)
    findings.add_column(no_wrap=True, style="dim")
    findings.add_column()
    for hit in report.rule_hits:
        findings.add_row(
            Text(f"  {hit.certainty}", style=_CONFIDENCE_STYLE[hit.certainty]),
            Text(hit.rule),
            Text(hit.finding),
        )
    console.print(findings)

    if show_evidence:
        console.print()
        print_evidence(console, report.target, report.evidence, header=False)


def prompt_json(prompt: Prompt) -> str:
    return json.dumps(
        {
            "system": prompt.system,
            "user": prompt.user,
            "est_tokens": prompt.est_tokens,
            "trimmed": prompt.trimmed,
        },
        indent=2,
    )


def print_prompt(console: Console, err: Console, prompt: Prompt) -> None:
    """The prompt verbatim (no Rich markup), with the size on stderr."""
    console.print(Text("--- system ---", style="dim"))
    console.print(Text(prompt.system))
    console.print(Text("--- user ---", style="dim"))
    console.print(Text(prompt.user))
    size = f"~{prompt.est_tokens} tokens. Nothing was sent."
    if prompt.trimmed:
        size += " Trimmed: " + "; ".join(prompt.trimmed) + "."
    err.print(Text(size, style="dim"))


def _answered_by(d: Diagnosis) -> str:
    if d.source == "llm":
        return f"AI ({d.model})"
    return "rules only" + (f" (based on {d.rule_hits[0]})" if d.rule_hits else "")


def report_markdown(report: Report) -> str:
    """For pasting into a ticket, PR or chat. Plain Markdown, no terminal styling."""
    d, t = report.diagnosis, report.target
    where = f"{t.kind.value}/{t.name} in `{t.namespace}`" + (f", pod `{t.pod}`" if t.pod else "")
    if t.kind is WorkloadKind.BUILD:
        where = f"docker build log `{t.name}`"
    lines = [
        f"### pfa diagnosis: {where}",
        "",
        f"**Failure type:** `{d.failure_type.value}`  ",
        f"**Confidence:** {d.confidence} · {_answered_by(d)}",
        "",
        f"**Problem:** {d.problem}",
        "",
        f"**Likely cause:** {d.likely_cause}",
    ]
    if d.fix:
        lines += ["", "**Fix:**", *(f"{i}. {step}" for i, step in enumerate(d.fix, 1))]
    if d.evidence_used:
        lines += ["", "**Evidence:**", *(f"- {e}" for e in d.evidence_used)]
    if report.notes:
        lines += ["", *(f"> {note}" for note in report.notes)]
    if report.rule_hits:
        lines += ["", "<details><summary>Rule findings</summary>", ""]
        lines += [f"- **{h.certainty}** `{h.rule}`: {h.finding}" for h in report.rule_hits]
        lines += ["", "</details>"]
    return "\n".join(lines)


def _config_rows(settings: Settings) -> list[tuple[str, str, str]]:
    rows = []
    for name in OPTIONS:
        value = getattr(settings, name)
        if name in SECRET_OPTIONS:
            shown = "set" if value else "not set"
        elif isinstance(value, bool):
            shown = "on" if value else "off"
        elif value is None:
            shown = "provider default" if name == "model" else "-"
        else:
            shown = str(value)
        rows.append((name, shown, settings.source(name)))
    return rows


def settings_json(settings: Settings, data_flow: str) -> str:
    return json.dumps(
        {
            "config_file": str(settings.config_path) if settings.config_path else None,
            "settings": {name: {"value": v, "source": s} for name, v, s in _config_rows(settings)},
            "data_flow": data_flow,
        },
        indent=2,
    )


def print_config(console: Console, settings: Settings, path: Path, data_flow: str) -> None:
    found = "" if settings.config_path else " (not found; create it with `pfa config init`)"
    console.print(Text(f"Config file: {path}{found}"))
    console.print()
    table = Table(box=None, padding=(0, 2), show_edge=False)
    table.add_column("setting", style="bold")
    table.add_column("value")
    table.add_column("from", style="dim")
    for name, value, source in _config_rows(settings):
        style = "yellow" if source != "default" else ""
        table.add_row(name, Text(value, style=style), source)
    console.print(table)
    console.print()
    console.print(Text("Data: ", style="bold") + Text(data_flow))


def eval_json(run: EvalRun) -> str:
    return run.model_dump_json(indent=2)


def eval_history_json(runs: list[EvalRun]) -> str:
    summaries = [r.model_dump(mode="json", exclude={"results"}) for r in runs]
    return json.dumps(summaries, indent=2)


def print_eval(
    console: Console, run: EvalRun, previous: EvalRun | None = None, saved: Path | None = None
) -> None:
    title = f"pfa eval: {run.answered_by}, {run.evidence} evidence"
    table = Table(title=title, box=None, padding=(0, 2), show_edge=False)
    table.add_column("case", style="bold", no_wrap=True)
    for column in ("type", "cause", "fix", "confidence"):
        table.add_column(column)
    table.add_column("seconds", justify="right")
    for r in run.results:
        table.add_row(
            r.case,
            _mark(r.type_ok),
            _mark(r.cause_ok),
            _mark(r.fix_ok),
            r.confidence,
            f"{r.seconds:.1f}",
        )
    console.print(table)
    console.print()

    console.print(
        Text(f"Correct: {run.correct}/{run.total}", style="bold") + _change(run, previous)
    )
    console.print(
        f"Failure type {run.type_correct}/{run.total} · cause {run.cause_correct}/{run.total} · "
        f"fix {run.fix_correct}/{run.total}"
    )
    console.print(
        Text(
            " · ".join(
                f"{name} {right}/{total}" for name, (right, total) in by_failure_type(run).items()
            )
        ),
        soft_wrap=True,
    )
    levels = by_difficulty(run)
    if "hard" in levels:
        console.print(
            Text(" · ".join(f"{name} {right}/{total}" for name, (right, total) in levels.items()))
        )
    console.print(Text(f"Confident and wrong: {run.confident_wrong}"))
    if run.errors:
        console.print(Text(f"No answer: {run.errors} (counted as wrong)", style="red"))

    if previous:
        fixed, broken = changes(run, previous)
        for label, names, style in (("Fixed", fixed, "green"), ("Broke", broken, "bold red")):
            if names:
                console.print(Text(f"{label} since then: {', '.join(names)}", style=style))

    misses = [r for r in run.results if not r.correct]
    if misses:
        console.print()
        console.print(Text("Misses", style="bold"))
    for r in misses:
        console.print(Text(f"  {r.case}: {_miss_reason(r)}"), soft_wrap=True)
    if saved:
        console.print()
        console.print(Text(f"Saved to {saved}", style="dim"), soft_wrap=True)


def print_eval_history(console: Console, runs: list[EvalRun]) -> None:
    if not runs:
        console.print("No saved runs yet. Run: pfa eval")
        return
    table = Table(box=None, padding=(0, 2), show_edge=False)
    table.add_column("date", no_wrap=True)
    table.add_column("answered by", style="bold", no_wrap=True)
    table.add_column("evidence")
    for column in ("correct", "confident wrong", "no answer"):
        table.add_column(column, justify="right")
    for run in runs:
        table.add_row(
            f"{run.date:%Y-%m-%d %H:%M}",
            run.answered_by,
            run.evidence,
            f"{run.correct}/{run.total}",
            str(run.confident_wrong),
            str(run.errors),
        )
    console.print(table)


def _mark(ok: bool) -> Text:
    return Text("yes", style="green") if ok else Text("NO", style="bold red")


def _change(run: EvalRun, previous: EvalRun | None) -> Text:
    if previous is None:
        return Text("")
    return Text(
        f" (previous run {previous.date:%Y-%m-%d}: {previous.correct}/{previous.total})",
        style="dim",
    )


def _miss_reason(r: CaseResult) -> str:
    if r.error:
        return r.error
    reasons = [] if r.type_ok else [f"answered {r.answered.value}, expected {r.expected.value}"]
    reasons += [f"missing {m}" for m in r.missing]
    if r.known_gap:
        reasons.append(f"known gap: {r.known_gap}")
    if r.cause:
        reasons.append(f'answered: "{r.cause[:200]}"')
    return "; ".join(reasons)
