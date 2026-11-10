"""pfa command-line entry point."""

from __future__ import annotations

from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from urllib3.exceptions import HTTPError

from pfa.ai.context import build_prompt
from pfa.ai.providers import LLMProvider, data_flow
from pfa.config import ConfigError, Settings, config_path, write_template
from pfa.engine.orchestrator import (
    analyze,
    answer,
    collect_evidence,
    default_collectors,
    load_evidence,
    run_diagnosis,
)
from pfa.engine.target import TargetNotFound, resolve_target
from pfa.evaluation import (
    EvalError,
    live,
    load_cases,
    load_runs,
    previous_run,
    replay,
    run_eval,
    save_run,
)
from pfa.kube import KubeClients, KubeConfigError, load_clients
from pfa.models import Evidence, Report, Target, WorkloadKind
from pfa.render import (
    eval_history_json,
    eval_json,
    evidence_json,
    print_config,
    print_diagnosis,
    print_eval,
    print_eval_history,
    print_evidence,
    print_prompt,
    prompt_json,
    report_json,
    report_markdown,
    settings_json,
)

app = typer.Typer(help="Platform Failure Assistant: diagnose Kubernetes and Docker failures.")
config_app = typer.Typer(help="Show or create the pfa configuration.")
app.add_typer(config_app, name="config")
console = Console()
err = Console(stderr=True)


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class DiagnosisFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    MARKDOWN = "markdown"  # for tickets, PRs and chat


NamespaceOpt = Annotated[str, typer.Option("--namespace", "-n")]
KindOpt = Annotated[
    WorkloadKind | None,
    typer.Option("--kind", "-k", help="Skip auto-detection and look up only this kind."),
]
ContextOpt = Annotated[
    str | None, typer.Option("--context", help="kubeconfig context (default: current).")
]
OutputOpt = Annotated[OutputFormat, typer.Option("--output", "-o")]


def _version(value: bool) -> None:
    if value:
        print(f"pfa {version('platform-failure-assistant')}")
        raise typer.Exit()


@app.callback()
def main(
    _: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    """Platform Failure Assistant. Read-only: never changes anything in the cluster."""


@app.command()
def collect(
    name: Annotated[str, typer.Argument(help="Pod, Deployment or StatefulSet name.")],
    namespace: NamespaceOpt = "default",
    kind: KindOpt = None,
    context: ContextOpt = None,
    output: OutputOpt = OutputFormat.TEXT,
) -> None:
    """Gather the (redacted) evidence a diagnosis would use, without calling any AI."""
    settings = _settings(kube_context=context)
    clients, target = _connect_and_resolve(settings, name, namespace, kind)
    with err.status("Collecting evidence..."):
        evidence = collect_evidence(target, default_collectors(clients, settings))

    if output is OutputFormat.JSON:
        print(evidence_json(target, evidence))
    else:
        print_evidence(console, target, evidence)


@app.command()
def diagnose(
    name: Annotated[str | None, typer.Argument(help="Pod, Deployment or StatefulSet name.")] = None,
    namespace: NamespaceOpt = "default",
    kind: KindOpt = None,
    context: ContextOpt = None,
    output: Annotated[DiagnosisFormat, typer.Option("--output", "-o")] = DiagnosisFormat.TEXT,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Rules only: no data leaves the machine.")
    ] = False,
    provider_name: Annotated[
        str | None,
        typer.Option("--provider", help="AI provider for this run: anthropic, ollama or none."),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Model for this run.")] = None,
    send_data: Annotated[
        bool | None,
        typer.Option(
            "--send-data/--no-send-data",
            help="Allow (or forbid) sending redacted evidence off this machine for this run.",
        ),
    ] = None,
    show_evidence: Annotated[
        bool, typer.Option("--show-evidence", help="Also print the evidence used.")
    ] = False,
    show_prompt: Annotated[
        bool,
        typer.Option(
            "--show-prompt",
            help="Print the exact (redacted) prompt the AI would get, then stop. Sends nothing.",
        ),
    ] = False,
    from_evidence: Annotated[
        Path | None,
        typer.Option(
            "--from-evidence",
            help="Diagnose a saved `pfa collect -o json` file instead of a live cluster.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Diagnose a failing pod or workload: problem, likely cause, fix and confidence."""
    if (name is None) == (from_evidence is None):
        _fail("give either a NAME or --from-evidence FILE", code=2)

    settings = _settings(
        kube_context=context, provider=provider_name, model=model, send_data=send_data
    )
    provider, no_ai_reason = None, None
    if not (no_llm or show_prompt):
        provider, no_ai_reason = _provider(settings)

    if from_evidence is not None:
        target, evidence = _load_evidence(from_evidence)
        report = analyze(target, evidence)
    else:
        clients, target = _connect_and_resolve(settings, name, namespace, kind)
        with err.status("Collecting evidence..."):
            report = run_diagnosis(target, default_collectors(clients, settings))

    if show_prompt:
        prompt = build_prompt(report, settings.prompt_budget_tokens)
        if output is DiagnosisFormat.JSON:
            print(prompt_json(prompt))
        else:
            print_prompt(console, err, prompt)
        return

    if provider is not None:
        report = _ask_ai(report, provider, settings)
    elif no_ai_reason:
        report = _with_note(report, no_ai_reason)

    if output is DiagnosisFormat.JSON:
        print(report_json(report, include_evidence=show_evidence))
    elif output is DiagnosisFormat.MARKDOWN:
        print(report_markdown(report))
    else:
        print_diagnosis(console, report, show_evidence=show_evidence)


@app.command("eval")
def eval_command(
    cases_dir: Annotated[
        Path, typer.Option("--cases", help="Folder of labelled cases.", file_okay=False)
    ] = Path("lab/cases"),
    only: Annotated[
        list[str] | None,
        typer.Option("--case", help="Score only this case (repeatable). Such runs are not saved."),
    ] = None,
    use_live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Collect evidence from the running lab (make lab-up) instead of the snapshots.",
        ),
    ] = False,
    namespace: Annotated[
        str, typer.Option("--namespace", "-n", help="Namespace of the lab, with --live.")
    ] = "pfa-lab",
    context: ContextOpt = None,
    results_dir: Annotated[
        Path, typer.Option("--results", help="Where runs are saved.", file_okay=False)
    ] = Path("eval/results"),
    save: Annotated[bool, typer.Option("--save/--no-save", help="Save this run.")] = True,
    history: Annotated[
        bool, typer.Option("--history", help="List the saved runs, then stop. Scores nothing.")
    ] = False,
    fail_under: Annotated[
        int | None,
        typer.Option(
            "--fail-under", min=0, max=100, help="Exit 1 if under this percentage is correct."
        ),
    ] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Score the rules only.")] = False,
    provider_name: Annotated[
        str | None,
        typer.Option("--provider", help="AI provider to score: anthropic, ollama or none."),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Model to score.")] = None,
    send_data: Annotated[
        bool | None,
        typer.Option(
            "--send-data/--no-send-data",
            help="Allow (or forbid) sending the cases' redacted evidence off this machine.",
        ),
    ] = None,
    output: OutputOpt = OutputFormat.TEXT,
) -> None:
    """Score the rules or a model against labelled cases and save the result."""
    if history:
        runs = load_runs(results_dir)
        if output is OutputFormat.JSON:
            print(eval_history_json(runs))
        else:
            print_eval_history(console, runs)
        return

    settings = _settings(
        kube_context=context, provider=provider_name, model=model, send_data=send_data
    )
    provider = None
    if not no_llm and settings.provider is not None:
        provider, flow = data_flow(settings)
        if provider is None:
            # Scoring the rules here would pass them off as the model the user asked for.
            _fail(flow, code=2)

    mode = "live" if use_live else "replay"
    try:
        cases = load_cases(cases_dir, only)
        if use_live:
            clients = load_clients(settings.kube_context, settings.api_timeout)
            diagnose = live(clients, settings, namespace)
        else:
            diagnose = replay(cases_dir)
        if provider is not None:
            where = "local" if provider.local else "redacted evidence leaves this machine"
            err.print(
                f"[dim]Asking {provider.model} at {provider.destination} about "
                f"{len(cases)} case(s) ({where}).[/]",
                soft_wrap=True,
            )
        with err.status("Scoring...") as status:
            run = run_eval(
                cases,
                diagnose,
                provider,
                settings.prompt_budget_tokens,
                on_case=lambda case: status.update(f"Scoring {case.id}..."),
                evidence=mode,
            )
    except KubeConfigError as exc:
        _fail(str(exc), code=2)
    except EvalError as exc:
        _fail(str(exc), code=2)

    # A partial run is not comparable with a full one, so it neither saves nor compares. A run
    # with no answers at all says the provider or cluster is down, not how good the answers are:
    # saved, it would become the baseline the next real run is compared with.
    full_run = not only
    nothing_answered = run.errors == run.total
    previous = previous_run(results_dir, run.answered_by, mode) if full_run else None
    saved = save_run(run, results_dir) if save and full_run and not nothing_answered else None
    if output is OutputFormat.JSON:
        print(eval_json(run))
    else:
        print_eval(console, run, previous, saved)

    if nothing_answered:
        _fail("no case got an answer, so this run says nothing about accuracy (not saved).", code=2)
    if fail_under is not None and run.correct * 100 < fail_under * run.total:
        _fail(f"{run.correct}/{run.total} correct is under --fail-under {fail_under}%.", code=1)


# --- pfa config -----------------------------------------------------------------------------


@config_app.callback(invoke_without_command=True)
def config_default(ctx: typer.Context) -> None:
    """Show the effective settings (same as `pfa config show`)."""
    if ctx.invoked_subcommand is None:
        config_show()


@config_app.command("show")
def config_show(output: OutputOpt = OutputFormat.TEXT) -> None:
    """Effective settings, where each one comes from, and what data leaves this machine."""
    settings = _settings()
    _, flow = data_flow(settings)
    if output is OutputFormat.JSON:
        print(settings_json(settings, flow))
    else:
        print_config(console, settings, config_path(), flow)


@config_app.command("init")
def config_init() -> None:
    """Create a commented config file (private to you: mode 600)."""
    path = config_path()
    if path.exists():
        _fail(f"{path} already exists; edit it, or delete it first.", code=1)
    write_template(path)
    console.print(f"Created {path}. Check the result with: pfa config show")


@config_app.command("path")
def config_path_cmd() -> None:
    """Print where pfa looks for its config file."""
    print(config_path())


# --- helpers --------------------------------------------------------------------------------


def _settings(**flags: object) -> Settings:
    try:
        return Settings.load(flags=flags)
    except ConfigError as exc:
        _fail(str(exc), code=2)


def _provider(settings: Settings) -> tuple[LLMProvider | None, str | None]:
    """The AI provider to use, or the reason there is none (shown as a note)."""
    provider, flow = data_flow(settings)
    if provider is not None:
        return provider, None
    if settings.provider is None:
        return None, (
            "No AI provider configured, so this is the rules answer. Set one with "
            "--provider, PFA_PROVIDER or `pfa config init`; silence this with --no-llm."
        )
    return None, flow


def _ask_ai(report: Report, provider: LLMProvider, settings: Settings) -> Report:
    # Say what is about to happen before it happens, every time data leaves the machine.
    if provider.local:
        err.print(
            f"[dim]Asking {provider.model} at {provider.destination} "
            "(local: nothing leaves this machine)[/]"
        )
    else:
        tokens = build_prompt(report, settings.prompt_budget_tokens).est_tokens
        err.print(
            f"[dim]Sending redacted evidence (~{tokens:,} tokens) to {provider.destination} "
            f"({provider.model}). See exactly what with --show-prompt.[/]"
        )
    with err.status(f"Waiting for {provider.model}..."):
        return answer(report, provider, settings.prompt_budget_tokens)


def _with_note(report: Report, note: str) -> Report:
    return report.model_copy(update={"notes": [*report.notes, note]})


def _connect_and_resolve(
    settings: Settings, name: str, namespace: str, kind: WorkloadKind | None
) -> tuple[KubeClients, Target]:
    try:
        clients = load_clients(settings.kube_context, settings.api_timeout)
        return clients, resolve_target(clients, name, namespace, kind)
    except KubeConfigError as exc:
        _fail(str(exc), code=2)
    except TargetNotFound as exc:
        _fail(f"{exc} (kube context: {_context_name(settings)})", code=1)
    except ApiException as exc:
        _fail(f"Kubernetes API {exc.status}: {exc.reason}", code=2)
    except HTTPError as exc:
        _fail(f"cannot reach the Kubernetes API: {exc}", code=2)


def _context_name(settings: Settings) -> str:
    if settings.kube_context:
        return settings.kube_context
    try:
        from kubernetes import config

        _, active = config.list_kube_config_contexts()
        return f"{active['name']} (current)"
    except Exception:  # noqa: BLE001 - only used to make an error message more helpful
        return "current"


def _load_evidence(path: Path) -> tuple[Target, list[Evidence]]:
    try:
        return load_evidence(path)
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
        _fail(f"{path} is not `pfa collect -o json` output: {exc}", code=2)


def _fail(message: str, code: int) -> NoReturn:
    # soft_wrap: no hard line breaks, so paths and commands in the message stay copyable.
    err.print(f"[bold red]error:[/] {escape(message)}", soft_wrap=True)
    raise typer.Exit(code)
