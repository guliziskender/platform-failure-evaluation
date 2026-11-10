"""Decide which kind of failure a target has, from pod state and events alone.

Order matters. A container killed by a failing liveness probe also shows CrashLoopBackOff
and exit code 137, so probe kills are checked before generic crashes, and OOMKilled (the
most specific termination reason) before both. A failing init container keeps the pod Pending,
so the Pending check leaves those to the OOM and crash checks.
"""

from __future__ import annotations

from collections.abc import Callable

from pfa.models import Classification, FailureType
from pfa.view import EvidenceView

IMAGE_PULL_REASONS = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}
START_ERROR_REASONS = {"CreateContainerConfigError", "CreateContainerError", "RunContainerError"}
# Exit codes after the kubelet kills a container: SIGKILL (137) or SIGTERM handled (143).
KILLED_EXIT_CODES = {137, 143}


def classify(view: EvidenceView) -> Classification:
    if view.pod is None:
        return _no_pod(view)
    for check in _CHECKS:
        result = check(view)
        if result is not None:
            return result
    return _unknown(view)


# --- checks, in priority order ----------------------------------------------------------


def _image_pull(v: EvidenceView) -> Classification | None:
    reason = v.waiting.get("reason")
    pull_failures = v.events_where("Failed", "Failed to pull image")
    if reason not in IMAGE_PULL_REASONS and not (pull_failures and not v.container.get("ready")):
        return None
    image = v.container.get("image", "?")
    shown = reason or "ErrImagePull"
    signals = [f"container {v.container_name} waiting: {shown}"]
    signals += [_event_signal(e) for e in pull_failures[:1]]
    return Classification(
        failure_type=FailureType.IMAGE_PULL,
        problem=f'{v.container_label.capitalize()} cannot start: image "{image}" fails to pull '
        f"({shown}).",
        signals=signals,
    )


def _pending(v: EvidenceView) -> Classification | None:
    if v.pod.get("phase") != "Pending":
        return None
    scheduled = v.condition("PodScheduled")
    if scheduled and scheduled["status"] == "False":
        message = scheduled.get("message") or scheduled.get("reason", "")
        return Classification(
            failure_type=FailureType.PENDING,
            problem=f"Pod {v.pod['name']} is Pending and has not been scheduled: {message}",
            signals=[f"PodScheduled=False ({scheduled.get('reason')})"]
            + [_event_signal(e) for e in v.events_where("FailedScheduling")[:1]],
        )
    reason = v.waiting.get("reason", "no container status yet")
    if reason in START_ERROR_REASONS:
        return None  # a config problem, handled as a crash below
    if v.is_init_container and _init_failing(v):
        return None  # the pod waits on a crashing init container: an OOM or a crash below
    return Classification(
        failure_type=FailureType.PENDING,
        problem=f"Pod {v.pod['name']} is scheduled but its containers have not started ({reason}).",
        signals=[f"phase=Pending, container waiting: {reason}"]
        + [_event_signal(e) for e in v.events_where("FailedMount")[:1]],
    )


def _oom(v: EvidenceView) -> Classification | None:
    if _up_and_recovered(v):
        return None  # an old OOM on a container that is fine now; _healthy mentions restarts
    for term in (v.terminated, v.last_terminated):
        if term.get("reason") == "OOMKilled":
            limit = v.container.get("resources", {}).get("limits", {}).get("memory")
            limit_text = f"; memory limit {limit}" if limit else "; no memory limit set"
            return Classification(
                failure_type=FailureType.OOM_KILLED,
                problem=f"{v.container_label.capitalize()} was OOMKilled (exit code "
                f"{term.get('exit_code', 137)}) and has restarted {v.restarts} times"
                f"{limit_text}.",
                signals=[f"termination: OOMKilled, exit {term.get('exit_code')}"],
            )
    return None


def _probe(v: EvidenceView) -> Classification | None:
    kills = [
        e
        for e in v.events_where("Killing")
        if "failed liveness probe" in e["message"] or "failed startup probe" in e["message"]
    ]
    unhealthy = v.events_where("Unhealthy")
    restart_probe = [
        e
        for e in unhealthy
        if e["message"].startswith(("Liveness probe failed", "Startup probe failed"))
    ]
    killed_by_kubelet = v.termination().get("exit_code") in KILLED_EXIT_CODES
    if kills or (restart_probe and v.restarts > 0 and killed_by_kubelet):
        kind = (
            "startup"
            if any("startup" in e["message"] for e in kills + restart_probe)
            else ("liveness")
        )
        return Classification(
            failure_type=FailureType.PROBE_FAILURE,
            problem=f"The {kind} probe of container {v.container_name} keeps failing, so the "
            f"kubelet restarts it ({v.restarts} restarts so far).",
            signals=[_event_signal(e) for e in (kills[:1] + restart_probe[:1])],
        )

    readiness = [e for e in unhealthy if e["message"].startswith("Readiness probe failed")]
    running = "running" in (v.container.get("state") or {})
    if readiness and running and not v.container.get("ready"):
        return Classification(
            failure_type=FailureType.PROBE_FAILURE,
            problem=f"Container {v.container_name} is running but not ready: its readiness "
            "probe keeps failing, so it receives no traffic.",
            signals=[_event_signal(readiness[0])],
        )
    return None


def _crash(v: EvidenceView) -> Classification | None:
    reason = v.waiting.get("reason")
    if reason in START_ERROR_REASONS:
        return Classification(
            failure_type=FailureType.CRASH_LOOP,
            problem=f"{v.container_label.capitalize()} cannot be started: {reason}: "
            f"{v.waiting.get('message', '')}".rstrip(": "),
            signals=[f"container waiting: {reason}"],
        )
    if _up_and_recovered(v):
        return None  # restarted in the past but fine now
    term = v.termination()
    crashed = term and (term.get("exit_code", 0) != 0 or v.restarts > 0)
    if reason != "CrashLoopBackOff" and not crashed:
        return None
    exit_code = term.get("exit_code", "?")
    last = "last exit code"
    if reason == "CrashLoopBackOff":
        state = "is in CrashLoopBackOff"
    elif "running" in (v.container.get("state") or {}):
        state = "keeps crashing (it is up right now, but restarts again)"
    elif v.restarts == 0:
        # One exit and no restart: a Job pod (restartPolicy Never/OnFailure) that failed.
        state, last = "exited and was not restarted", "exit code"
    else:
        state = "keeps exiting"
    blocked = ", so the pod's other containers never start" if v.is_init_container else ""
    return Classification(
        failure_type=FailureType.CRASH_LOOP,
        problem=f"{v.container_label.capitalize()} {state}: {last} {exit_code} "
        f"({term.get('reason', 'no reason')}), {v.restarts} restarts{blocked}.",
        signals=[f"termination: {term.get('reason')}, exit {exit_code}"]
        + ([f"container waiting: {reason}"] if reason else []),
    )


def _healthy(v: EvidenceView) -> Classification | None:
    if v.failed_recently():
        return None  # up right now, but in the middle of a crash loop
    phase = v.pod.get("phase")
    containers = v.pod.get("containers", [])
    if phase == "Succeeded" or (phase == "Running" and all(c.get("ready") for c in containers)):
        restarts = sum(c.get("restart_count", 0) for c in containers)
        note = f" It has restarted {restarts} times in the past." if restarts else ""
        if phase == "Succeeded":  # a finished Job pod: nothing is running, and that is fine
            state, signal = "completed successfully (phase Succeeded)", "all containers exited 0"
        else:
            state, signal = "Running and all containers are ready", "all containers ready"
        return Classification(
            failure_type=FailureType.NONE,
            problem=f"No failure detected: pod {v.pod['name']} {state}.{note}",
            signals=[f"phase={phase}", signal],
        )
    return None


_CHECKS: list[Callable[[EvidenceView], Classification | None]] = [
    _image_pull,
    _pending,
    _oom,
    _probe,
    _crash,
    _healthy,
]


# --- fallbacks --------------------------------------------------------------------------


def _init_failing(v: EvidenceView) -> bool:
    term = v.termination()
    return (
        v.waiting.get("reason") == "CrashLoopBackOff"
        or term.get("exit_code", 0) != 0
        or term.get("reason") == "OOMKilled"
    )


def _up_and_recovered(v: EvidenceView) -> bool:
    """Running and ready, and no abnormal exit recently: past failures don't count."""
    running = "running" in (v.container.get("state") or {})
    return running and bool(v.container.get("ready")) and not v.failed_recently()


def _no_pod(v: EvidenceView) -> Classification:
    t = v.target
    if t.pod is not None:
        return Classification(
            failure_type=FailureType.UNKNOWN,
            problem=f"Could not read the state of pod {t.pod}.",
            signals=[ev.error for ev in v.evidence if ev.error],
        )
    failed = v.events_where("FailedCreate")
    detail = f" {failed[0]['message']}" if failed else ""
    return Classification(
        failure_type=FailureType.UNKNOWN,
        problem=f"{t.kind.value.capitalize()} {t.name} has no pods.{detail}",
        signals=[_event_signal(e) for e in failed[:1]] or ["no pods match the workload"],
    )


def _unknown(v: EvidenceView) -> Classification:
    phase = v.pod.get("phase")
    reason = v.pod.get("reason")
    detail = f" ({reason}: {v.pod.get('message', '')})" if reason else ""
    return Classification(
        failure_type=FailureType.UNKNOWN,
        problem=f"Pod {v.pod['name']} is {phase}{detail}; container {v.container_name} is "
        "not ready.",
        signals=[f"phase={phase}"] + ([f"pod reason={reason}"] if reason else []),
    )


def _event_signal(e: dict) -> str:
    count = f" x{e['count']}" if e.get("count", 1) > 1 else ""
    return f"event {e['reason']}{count}: {e['message']}"
