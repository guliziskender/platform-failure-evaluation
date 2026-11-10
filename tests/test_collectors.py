import json
from datetime import UTC, datetime

from kubernetes.client.exceptions import ApiException

from pfa.collectors.base import run_safely
from pfa.collectors.k8s_events import K8sEventsCollector, dedupe_events
from pfa.collectors.k8s_state import K8sStateCollector, summarize_pod
from pfa.collectors.logs import LogsCollector, trim_tail
from pfa.engine.orchestrator import collect_evidence
from pfa.models import Evidence, FailureType, OwnerRef, Target, WorkloadKind

from .conftest import FakeCore, load_json, make_clients, pod, to_k8s


def _target(pod_name="p", container="app", owners=()):
    return Target(
        namespace="default",
        name=pod_name,
        kind=WorkloadKind.POD,
        pod=pod_name,
        container=container,
        owners=list(owners),
    )


# --- k8s_state -----------------------------------------------------------------------------


def test_state_captures_oomkill_facts(oom_pod):
    c = summarize_pod(oom_pod)["containers"][0]
    assert c["last_state"]["terminated"]["reason"] == "OOMKilled"
    assert c["last_state"]["terminated"]["exit_code"] == 137
    assert c["resources"]["limits"]["memory"] == "100Mi"
    assert c["restart_count"] > 0


def test_state_keeps_env_names_but_never_values():
    crash = pod("crashloop")
    summary = summarize_pod(crash)

    assert summary["containers"][0]["env_names"] == ["DB_PASSWORD"]
    assert "pfa-fake-secret-value" not in json.dumps(summary)


def test_state_pending_pod_reports_unschedulable():
    summary = summarize_pod(pod("pending"))
    scheduled = next(c for c in summary["conditions"] if c["type"] == "PodScheduled")
    assert scheduled["reason"] == "Unschedulable"
    assert "Insufficient cpu" in scheduled["message"]
    assert summary["containers"][0]["resources"]["requests"]["cpu"] == "64"


def test_state_summarizes_probe():
    probe = summarize_pod(pod("badprobe"))["containers"][0]["probes"]["liveness"]
    assert probe["http_get"] == "HTTP /healthz port=8080"
    assert probe["failure_threshold"] == 1


def test_state_lists_container_ports_with_names():
    def add_ports(data):
        data["spec"]["containers"][0]["ports"] = [
            {"name": "http", "containerPort": 80, "protocol": "TCP"},
            {"containerPort": 9090, "protocol": "TCP"},
        ]

    ports = summarize_pod(pod("badprobe", add_ports))["containers"][0]["ports"]
    assert ports == ["http=80/TCP", "9090/TCP"]


def test_state_collector_without_pod_notes_it():
    target = Target(namespace="default", name="x", kind=WorkloadKind.POD)
    ev = K8sStateCollector(make_clients()).collect(target)
    assert ev.summary["note"] == "workload has no pods"


# --- k8s_events ----------------------------------------------------------------------------


def _events():
    return [to_k8s(e, "CoreV1Event") for e in load_json("events_memhog.json")["items"]]


def test_events_from_fixture_are_deduped_warnings_first():
    events = dedupe_events(_events(), since=datetime(2026, 10, 9, tzinfo=UTC))
    keys = [(e["object"], e["reason"], e["message"]) for e in events]

    assert len(keys) == len(set(keys))
    assert events[0]["type"] == "Warning"
    backoff = next(e for e in events if e["reason"] == "BackOff")
    assert backoff["count"] > 1


def test_events_merge_messages_that_differ_only_in_numbers():
    def ev(msg, ts):
        return to_k8s(
            {
                "metadata": {"name": "e", "namespace": "default"},
                "involvedObject": {"kind": "Pod", "name": "p"},
                "reason": "Pulled",
                "type": "Normal",
                "message": msg,
                "count": 1,
                "lastTimestamp": ts,
                "firstTimestamp": ts,
            },
            "CoreV1Event",
        )

    merged = dedupe_events(
        [
            ev('pulled "img" in 813ms. Image size: 4041495 bytes.', "2026-10-09T11:00:00Z"),
            ev('pulled "img" in 1.137s. Image size: 4041495 bytes.', "2026-10-09T11:05:00Z"),
        ],
        since=datetime(2026, 10, 9, tzinfo=UTC),
    )
    assert len(merged) == 1
    assert merged[0]["count"] == 2
    assert "1.137s" in merged[0]["message"]  # newest wording wins


def test_events_outside_window_are_dropped():
    assert dedupe_events(_events(), since=datetime(2030, 1, 1, tzinfo=UTC)) == []


def test_events_collector_only_keeps_matching_kinds(oom_pod):
    core = FakeCore(events=_events())
    target = _target(oom_pod.metadata.name, owners=[OwnerRef(kind="Deployment", name="memhog")])

    ev = K8sEventsCollector(make_clients(core), window_minutes=10**7).collect(target)

    objects = {e["object"] for e in ev.summary["events"]}
    assert objects <= {f"Pod/{oom_pod.metadata.name}", "Deployment/memhog"}
    assert "Deployment/memhog" in objects


# --- logs ----------------------------------------------------------------------------------


def test_logs_previous_first_and_missing_previous_is_not_an_error():
    no_previous = ApiException(status=400, reason="Bad Request")
    no_previous.body = json.dumps({"message": 'previous terminated container "app" not found'})
    core = FakeCore(logs={("p", True): no_previous, ("p", False): "boot\nfatal: no config\n"})

    ev = LogsCollector(make_clients(core)).collect(_target())

    assert ev.error is None
    assert ev.summary["previous"] == {
        "available": False,
        "note": 'previous terminated container "app" not found',
    }
    assert ev.raw_excerpt.startswith("=== current container ===\nboot")


def test_logs_kubelet_garbage_collected_text_is_unavailable():
    gone = "unable to retrieve container logs for containerd://abc123"
    core = FakeCore(logs={("p", True): gone, ("p", False): "ok\n"})

    ev = LogsCollector(make_clients(core)).collect(_target())

    assert ev.summary["previous"]["available"] is False
    assert "containerd" not in ev.raw_excerpt


def test_logs_are_trimmed_to_tail():
    text = "".join(f"line {i}\n" for i in range(1000))
    core = FakeCore(logs={("p", False): text})

    ev = LogsCollector(make_clients(core), max_bytes=100).collect(_target())

    assert ev.summary["current"]["truncated"] is True
    assert ev.raw_excerpt.rstrip().endswith("line 999")


def test_trim_tail_cuts_at_line_boundary():
    trimmed, truncated = trim_tail("aaaa\nbbbb\ncccc\n", max_bytes=8)
    assert truncated
    assert trimmed == "cccc\n"
    assert trim_tail("short", 100) == ("short", False)


# --- safety and orchestration --------------------------------------------------------------


class _Boom:
    name = "boom"

    def applies_to(self, failure):
        return True

    def collect(self, target):
        raise ApiException(status=403, reason="Forbidden")


class _Ok:
    name = "ok"

    def applies_to(self, failure):
        return failure is not FailureType.DOCKER_BUILD

    def collect(self, target):
        return Evidence(source=self.name, summary={"fine": True})


def test_run_safely_turns_exceptions_into_evidence():
    ev = run_safely(_Boom(), _target())
    assert ev.source == "boom"
    assert ev.error == "Kubernetes API 403: Forbidden"


def test_one_failing_collector_does_not_break_the_others():
    evidence = collect_evidence(_target(), [_Boom(), _Ok()])
    assert [e.source for e in evidence] == ["boom", "ok"]
    assert evidence[0].error and evidence[1].summary == {"fine": True}


def test_collectors_filtered_by_failure_type():
    evidence = collect_evidence(_target(), [_Boom(), _Ok()], FailureType.DOCKER_BUILD)
    assert [e.source for e in evidence] == ["boom"]
