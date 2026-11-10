import pytest
from kubernetes.client import V1LabelSelector, V1LabelSelectorRequirement

from pfa.engine.target import (
    TargetNotFound,
    label_selector,
    pick_container,
    pick_pod,
    resolve_target,
)
from pfa.models import WorkloadKind

from .conftest import FakeApps, FakeCore, deployment, make_clients, pod, replicaset

RS_UID = "1e0658b4-5b85-4a74-891a-cc3abcf38652"  # owner of the captured memhog pod
DEPLOY_UID = "deploy-uid"


def _healthy(data):
    """Turn the OOMKilled fixture into a healthy, never-restarted sibling pod."""
    data["metadata"]["name"] = "memhog-healthy"
    cs = data["status"]["containerStatuses"][0]
    cs.update({"ready": True, "restartCount": 0, "lastState": {}})
    cs["state"] = {"running": {"startedAt": "2026-10-09T11:40:00Z"}}
    data["status"]["phase"] = "Running"


def test_pick_pod_prefers_the_failing_one(oom_pod):
    healthy = pod("oomkilled", _healthy)
    assert pick_pod([healthy, oom_pod]).metadata.name == oom_pod.metadata.name
    assert pick_pod([]) is None


def _terminating(data):
    data["metadata"]["deletionTimestamp"] = "2026-10-09T11:45:00Z"


def test_pick_pod_skips_a_failing_pod_that_is_shutting_down():
    # After a fix rolls out: the old pod still crash-loops while it terminates.
    old = pod("oomkilled", _terminating)
    new = pod("oomkilled", _healthy)
    assert pick_pod([old, new]).metadata.name == "memhog-healthy"


def test_pick_pod_uses_a_terminating_pod_when_there_is_nothing_else():
    old = pod("oomkilled", _terminating)
    assert pick_pod([old]) is old


def test_pick_container_returns_crashing_container(oom_pod):
    assert pick_container(oom_pod) == "stress"


def test_pick_container_on_pending_pod_falls_back_to_first_spec_container():
    assert pick_container(pod("pending")) == "app"


def test_pick_container_prefers_failing_init_container():
    def add_failing_init(data):
        data["spec"]["initContainers"] = [{"name": "migrate", "image": "busybox"}]
        data["status"]["initContainerStatuses"] = [
            {
                "name": "migrate",
                "image": "busybox",
                "imageID": "",
                "ready": False,
                "restartCount": 4,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {
                    "terminated": {
                        "exitCode": 1,
                        "reason": "Error",
                        "finishedAt": "2026-10-09T12:00:00Z",
                    }
                },
            }
        ]
        # Main container is just waiting for init to finish.
        waiting = {"waiting": {"reason": "PodInitializing"}}
        data["status"]["containerStatuses"][0].update(
            {"restartCount": 0, "lastState": {}, "state": waiting}
        )

    assert pick_container(pod("oomkilled", add_failing_init)) == "migrate"


def test_resolve_pod_walks_owner_chain(oom_pod):
    rs = replicaset("memhog-7957d7d5c8", RS_UID, DEPLOY_UID, "memhog", revision=2)
    clients = make_clients(FakeCore(pods=[oom_pod]), FakeApps(replicasets=[rs]))

    target = resolve_target(clients, oom_pod.metadata.name, "default")

    assert target.kind is WorkloadKind.POD
    assert target.container == "stress"
    assert [(o.kind, o.name) for o in target.owners] == [
        ("ReplicaSet", "memhog-7957d7d5c8"),
        ("Deployment", "memhog"),
    ]


def test_resolve_deployment_picks_failing_pod(oom_pod):
    healthy = pod("oomkilled", _healthy)
    dep = deployment("memhog", DEPLOY_UID, {"app": "memhog"})
    rs = replicaset("memhog-7957d7d5c8", RS_UID, DEPLOY_UID, "memhog", revision=2)
    clients = make_clients(
        FakeCore(pods=[healthy, oom_pod]), FakeApps(deployments=[dep], replicasets=[rs])
    )

    target = resolve_target(clients, "memhog", "default")

    assert target.kind is WorkloadKind.DEPLOYMENT
    assert target.pod == oom_pod.metadata.name
    assert target.owners[0].name == "memhog-7957d7d5c8"


def test_resolve_deployment_without_pods_keeps_newest_replicaset():
    dep = deployment("api", DEPLOY_UID, {"app": "api"})
    old = replicaset("api-old", "rs-1", DEPLOY_UID, "api", revision=1)
    new = replicaset("api-new", "rs-2", DEPLOY_UID, "api", revision=2)
    clients = make_clients(FakeCore(), FakeApps(deployments=[dep], replicasets=[old, new]))

    target = resolve_target(clients, "api", "default", WorkloadKind.DEPLOYMENT)

    assert target.pod is None
    assert [(o.kind, o.name) for o in target.owners] == [
        ("ReplicaSet", "api-new"),
        ("Deployment", "api"),
    ]


def test_resolve_ignores_pods_of_other_workloads_with_same_labels(oom_pod):
    # Same labels, but the pod's ReplicaSet belongs to a different Deployment.
    dep = deployment("memhog", DEPLOY_UID, {"app": "memhog"})
    clients = make_clients(FakeCore(pods=[oom_pod]), FakeApps(deployments=[dep]))

    assert resolve_target(clients, "memhog", "default").pod is None


def test_resolve_unknown_name_raises():
    with pytest.raises(TargetNotFound, match="nope"):
        resolve_target(make_clients(), "nope", "default")


def test_label_selector_with_expressions():
    sel = V1LabelSelector(
        match_labels={"app": "web"},
        match_expressions=[
            V1LabelSelectorRequirement(key="tier", operator="In", values=["a", "b"]),
            V1LabelSelectorRequirement(key="canary", operator="DoesNotExist"),
        ],
    )
    assert label_selector(sel) == "app=web,tier in (a,b),!canary"
