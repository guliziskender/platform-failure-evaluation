"""Broken-pod lab: create, inspect and delete broken apps, three per pod failure type.

    uv run python lab/lab.py up       # apply all cases, wait until each shows its failure
    uv run python lab/lab.py status   # which cases are broken the way they should be
    uv run python lab/lab.py down     # delete everything (the pfa-lab namespace)
    uv run python lab/lab.py reset    # down + up: fresh restart counts and events
    uv run python lab/lab.py snapshot # save each case's `pfa collect` evidence for offline tests

This is a dev tool, deliberately outside the `pfa` package: pfa itself never writes to a
cluster. It refuses to run against anything but a local cluster (kind, minikube, Docker
Desktop, k3d) unless PFA_LAB_ALLOW_CONTEXT=1 is set.

Every command uses one kube context, the same one `pfa` uses: PFA_KUBE_CONTEXT or kube_context
in pfa's config file if set, else kubectl's current context. So `up` and `snapshot` never look
at different clusters.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

LAB_DIR = Path(__file__).resolve().parent
CASES_DIR = LAB_DIR / "cases"
NAMESPACE = "pfa-lab"
LOCAL_CONTEXT_PREFIXES = ("kind-", "minikube", "docker-desktop", "k3d-")

console = Console()


@dataclass
class Case:
    id: str  # folder name
    kind: str
    name: str
    failure_type: str
    ready_when: dict[str, Any]
    expected: dict[str, Any]


def load_cases() -> list[Case]:
    cases = []
    for path in sorted(CASES_DIR.glob("*/expected.yaml")):
        exp = yaml.safe_load(path.read_text())
        cases.append(
            Case(
                id=path.parent.name,
                kind=exp["workload"]["kind"],
                name=exp["workload"]["name"],
                failure_type=exp["failure_type"],
                ready_when=exp["ready_when"],
                expected=exp,
            )
        )
    return cases


# --- kubectl --------------------------------------------------------------------------------


@cache
def lab_context() -> str:
    from pfa.config import ConfigError, Settings

    try:
        configured = Settings.load().kube_context
    except ConfigError as exc:
        console.print(f"[red]pfa config error:[/] {exc}")
        sys.exit(2)
    if configured:
        return configured
    result = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        console.print(f"[red]No current kube context:[/] {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout.strip()


def kubectl(*args: str, check: bool = True) -> str:
    command = ["kubectl", "--context", lab_context(), *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        console.print(f"[red]kubectl {' '.join(args)} failed:[/]\n{result.stderr.strip()}")
        sys.exit(1)
    return result.stdout


def guard_context() -> str:
    context = lab_context()
    if (
        not context.startswith(LOCAL_CONTEXT_PREFIXES)
        and os.environ.get("PFA_LAB_ALLOW_CONTEXT") != "1"
    ):
        console.print(
            f"[red]Refusing to deploy broken apps to context {context!r}.[/] The lab is meant "
            "for local clusters (kind, minikube, Docker Desktop, k3d). "
            "Set PFA_LAB_ALLOW_CONTEXT=1 to override."
        )
        sys.exit(2)
    return context


# --- state checks ---------------------------------------------------------------------------


def lab_pods() -> list[dict[str, Any]]:
    out = kubectl("get", "pods", "-n", NAMESPACE, "-o", "json", check=False)
    return json.loads(out)["items"] if out else []


def lab_workloads() -> dict[tuple[str, str], dict[str, Any]]:
    out = kubectl("get", "deployments,statefulsets", "-n", NAMESPACE, "-o", "json", check=False)
    items = json.loads(out)["items"] if out else []
    return {(w["kind"].lower(), w["metadata"]["name"]): w for w in items}


def pods_for(case: Case, pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        p
        for p in pods
        if p["metadata"].get("labels", {}).get("app") == case.name
        and not p["metadata"].get("deletionTimestamp")
    ]


def rolled_out(case: Case, workloads: dict[tuple[str, str], dict[str, Any]]) -> bool:
    """Every pod runs the current spec. Until then an old pod from before a manifest change
    can match `ready_when`, and tests would read stale evidence."""
    w = workloads.get((case.kind, case.name))
    if w is None:
        return False
    status, wanted = w.get("status", {}), w["spec"].get("replicas", 1)
    return (
        status.get("observedGeneration", 0) >= w["metadata"]["generation"]
        and status.get("updatedReplicas", 0) == wanted
        and status.get("replicas", 0) == wanted
    )


def is_ready(
    case: Case, pods: list[dict[str, Any]], workloads: dict[tuple[str, str], dict[str, Any]]
) -> bool:
    """True when the case is rolled out and one of its pods shows the `ready_when` failure."""
    return rolled_out(case, workloads) and any(
        _pod_matches(case.ready_when, p) for p in pods_for(case, pods)
    )


def _pod_matches(rule: dict[str, Any], pod: dict[str, Any]) -> bool:
    statuses = _statuses(pod)
    if "restarts_at_least" in rule:
        if sum(cs.get("restartCount", 0) for cs in statuses) < rule["restarts_at_least"]:
            return False
    if "waiting_reason" in rule:
        reasons = {cs.get("state", {}).get("waiting", {}).get("reason") for cs in statuses}
        if not reasons & set(rule["waiting_reason"]):
            return False
    if "last_terminated_reason" in rule:
        reasons = {cs.get("lastState", {}).get("terminated", {}).get("reason") for cs in statuses}
        reasons |= {cs.get("state", {}).get("terminated", {}).get("reason") for cs in statuses}
        if rule["last_terminated_reason"] not in reasons:
            return False
    if "pod_condition_reason" in rule:
        reasons = {c.get("reason") for c in pod["status"].get("conditions", [])}
        if rule["pod_condition_reason"] not in reasons:
            return False
    return True


def _statuses(pod: dict[str, Any]) -> list[dict[str, Any]]:
    status = pod["status"]
    return status.get("initContainerStatuses", []) + status.get("containerStatuses", [])


def pod_status(pods: list[dict[str, Any]]) -> str:
    """Short status like kubectl's STATUS column, plus restarts."""
    if not pods:
        return "no pods"
    parts = []
    for p in pods:
        statuses = _statuses(p)
        reason = p["status"].get("phase", "?")
        for cs in statuses:
            state = cs.get("state", {})
            reason = (state.get("waiting") or state.get("terminated") or {}).get("reason", reason)
        restarts = sum(cs.get("restartCount", 0) for cs in statuses)
        parts.append(f"{reason} ({restarts} restarts)" if restarts else reason)
    return ", ".join(parts)


# --- commands -------------------------------------------------------------------------------


def cmd_up(args: argparse.Namespace) -> int:
    context = guard_context()
    console.print(f"Applying lab to context [bold]{context}[/], namespace [bold]{NAMESPACE}[/]")
    console.print(kubectl("apply", "-k", str(LAB_DIR)).rstrip(), style="dim")
    if args.no_wait:
        return 0
    return wait_until_broken(load_cases(), args.timeout)


def wait_until_broken(cases: list[Case], timeout: float) -> int:
    deadline = time.monotonic() + timeout
    pending = {c.id for c in cases}
    with console.status("Waiting for every app to break the way it should...") as status:
        while pending and time.monotonic() < deadline:
            pods, workloads = lab_pods(), lab_workloads()
            for case in cases:
                if case.id in pending and is_ready(case, pods, workloads):
                    pending.discard(case.id)
                    console.print(f"  [green]✓[/] {case.id}")
            status.update(f"Waiting for: {', '.join(sorted(pending))}")
            if pending:
                time.sleep(3)
    print_status(cases)
    if pending:
        console.print(
            f"[red]Timed out after {timeout:.0f}s waiting for:[/] {', '.join(sorted(pending))}"
        )
        return 1
    console.print("[green]Lab is ready.[/] Try: uv run pfa collect checkout-api -n pfa-lab")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    print_status(load_cases())
    return 0


def print_status(cases: list[Case]) -> None:
    pods, workloads = lab_pods(), lab_workloads()
    table = Table(title=f"pfa lab ({NAMESPACE}, context {lab_context()})")
    for col in ("case", "workload", "failure type", "pod status", "ready"):
        table.add_column(col)
    for case in cases:
        ready = is_ready(case, pods, workloads)
        table.add_row(
            case.id,
            f"{case.kind}/{case.name}",
            case.failure_type,
            pod_status(pods_for(case, pods)),
            "[green]yes[/]" if ready else "[yellow]not yet[/]",
        )
    console.print(table)


def cmd_down(_: argparse.Namespace) -> int:
    guard_context()
    console.print(f"Deleting namespace {NAMESPACE} (all lab apps)...")
    kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true")
    console.print("[green]Lab removed.[/]")
    return 0


def cmd_snapshot(_: argparse.Namespace) -> int:
    """Save `pfa collect -o json` for every case to cases/<id>/evidence.json.

    Snapshots let the classifier and rules be tested offline against expected.yaml, and are
    the replayable input for the evaluation suite. Reads the cluster through pfa (read-only).
    """
    from pfa.config import Settings
    from pfa.engine.orchestrator import collect_evidence, default_collectors
    from pfa.engine.target import resolve_target
    from pfa.kube import load_clients
    from pfa.render import evidence_json

    cases = load_cases()
    pods, workloads = lab_pods(), lab_workloads()
    not_ready = [c.id for c in cases if not is_ready(c, pods, workloads)]
    if not_ready:
        console.print(f"[red]Not broken yet:[/] {', '.join(not_ready)}. Run `make lab-up` first.")
        return 1
    settings = Settings.load()
    clients = load_clients(lab_context(), settings.api_timeout)
    for case in cases:
        target = resolve_target(clients, case.name, NAMESPACE)
        evidence = collect_evidence(target, default_collectors(clients, settings))
        path = CASES_DIR / case.id / "evidence.json"
        path.write_text(evidence_json(target, evidence) + "\n")
        console.print(f"  [green]✓[/] {path.relative_to(LAB_DIR.parent)}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    cmd_down(args)
    return cmd_up(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func, help_text in (
        ("up", cmd_up, "apply all cases and wait until each is broken"),
        ("reset", cmd_reset, "delete and recreate the lab"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--no-wait", action="store_true", help="apply and return immediately")
        p.add_argument("--timeout", type=float, default=180, help="seconds to wait (default 180)")
        p.set_defaults(func=func)
    sub.add_parser("status", help="show each case and whether it is broken yet").set_defaults(
        func=cmd_status
    )
    sub.add_parser("down", help="delete the lab namespace").set_defaults(func=cmd_down)
    sub.add_parser(
        "snapshot", help="save each case's evidence to cases/<id>/evidence.json"
    ).set_defaults(func=cmd_snapshot)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
