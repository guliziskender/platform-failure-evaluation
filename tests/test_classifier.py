from pfa.engine.classifier import classify
from pfa.models import FailureType

from .conftest import crashed, make_view, waiting

LIVENESS_KILL = {
    "type": "Normal",
    "reason": "Killing",
    "message": "Container app failed liveness probe, will be restarted",
}
LIVENESS_FAIL = {
    "reason": "Unhealthy",
    "message": 'Liveness probe failed: Get "http://10.0.0.1:8080/healthz": connect: connection '
    "refused",
}


def test_probe_kill_is_not_mistaken_for_a_crash():
    # Real surface of a liveness kill: CrashLoopBackOff + exit 137 "Error", like a crash or OOM.
    view = make_view(crashed(137), events=[LIVENESS_FAIL, LIVENESS_KILL])
    assert classify(view).failure_type is FailureType.PROBE_FAILURE


def test_probe_failure_without_killing_event_still_detected():
    view = make_view(crashed(137), events=[LIVENESS_FAIL])
    assert classify(view).failure_type is FailureType.PROBE_FAILURE


def test_app_crash_with_failing_probe_stays_a_crash():
    # Exit 1 means the app died on its own; the probe failures are a side effect.
    view = make_view(crashed(1), events=[LIVENESS_FAIL])
    assert classify(view).failure_type is FailureType.CRASH_LOOP


def test_oom_beats_crashloopbackoff():
    view = make_view(crashed(137, reason="OOMKilled"))
    result = classify(view)
    assert result.failure_type is FailureType.OOM_KILLED
    assert "OOMKilled" in result.problem


def test_readiness_failure_on_running_container():
    container = {"state": {"running": {}}, "ready": False, "restart_count": 0}
    events = [{"reason": "Unhealthy", "message": "Readiness probe failed: HTTP probe failed"}]
    result = classify(make_view(container, events=events))
    assert result.failure_type is FailureType.PROBE_FAILURE
    assert "not ready" in result.problem


def test_image_pull():
    result = classify(make_view(waiting("ImagePullBackOff"), pod={"phase": "Pending"}))
    assert result.failure_type is FailureType.IMAGE_PULL
    assert "registry.example.com/app:1.0" in result.problem


def test_unscheduled_pod_is_pending():
    pod = {
        "phase": "Pending",
        "conditions": [
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/3 nodes are available: 3 Insufficient memory.",
            }
        ],
    }
    result = classify(make_view({}, pod=pod))
    assert result.failure_type is FailureType.PENDING
    assert "Insufficient memory" in result.problem


def test_scheduled_but_not_started_is_pending():
    result = classify(make_view(waiting("ContainerCreating"), pod={"phase": "Pending"}))
    assert result.failure_type is FailureType.PENDING
    assert "ContainerCreating" in result.problem


def test_create_container_config_error_is_a_crash_not_pending():
    container = waiting("CreateContainerConfigError", 'secret "db" not found')
    result = classify(make_view(container, pod={"phase": "Pending"}))
    assert result.failure_type is FailureType.CRASH_LOOP


def test_healthy_pod_with_old_restarts_is_none():
    container = {
        "state": {"running": {}},
        "ready": True,
        "restart_count": 2,
        "last_state": {"terminated": {"reason": "Error", "exit_code": 1}},
    }
    result = classify(make_view(container))
    assert result.failure_type is FailureType.NONE
    assert "restarted 2 times" in result.problem


def test_workload_without_pods_reports_failed_create():
    events = [{"reason": "FailedCreate", "message": 'pods "app-x" is forbidden: exceeded quota'}]
    result = classify(make_view(has_pod=False, events=events))
    assert result.failure_type is FailureType.UNKNOWN
    assert "has no pods" in result.problem
    assert "exceeded quota" in result.problem


def test_evicted_pod_is_unknown():
    pod = {"phase": "Failed", "reason": "Evicted", "message": "The node was low on memory."}
    result = classify(make_view({"state": {}}, pod=pod))
    assert result.failure_type is FailureType.UNKNOWN
    assert "Evicted" in result.problem


# --- running right now, but failing recently (crash loop "up window") ---------------------


def _ago(**delta) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _up_after(exit_code: int, reason: str, finished_at: str, restarts: int = 5) -> dict:
    """Container that is running and ready now, after an earlier termination."""
    return {
        "state": {"running": {}},
        "ready": True,
        "restart_count": restarts,
        "last_state": {
            "terminated": {"reason": reason, "exit_code": exit_code, "finished_at": finished_at}
        },
    }


def test_crash_loop_caught_while_container_is_up_via_backoff_event():
    # Regression: this was reported as "No failure detected".
    backoff = {"reason": "BackOff", "message": "Back-off restarting failed container app in pod"}
    result = classify(make_view(_up_after(1, "Error", _ago(hours=5)), events=[backoff]))
    assert result.failure_type is FailureType.CRASH_LOOP
    assert "up right now" in result.problem


def test_crash_loop_caught_while_container_is_up_via_recent_exit():
    result = classify(make_view(_up_after(1, "Error", _ago(minutes=2))))
    assert result.failure_type is FailureType.CRASH_LOOP


def test_recent_oom_on_running_container_is_still_oom():
    result = classify(make_view(_up_after(137, "OOMKilled", _ago(minutes=3))))
    assert result.failure_type is FailureType.OOM_KILLED


def test_old_oom_on_recovered_container_is_healthy():
    result = classify(make_view(_up_after(137, "OOMKilled", _ago(days=2), restarts=1)))
    assert result.failure_type is FailureType.NONE
    assert "restarted 1 times" in result.problem


def test_old_crash_on_recovered_container_is_healthy():
    result = classify(make_view(_up_after(1, "Error", _ago(hours=3), restarts=2)))
    assert result.failure_type is FailureType.NONE
