"""Fixtures: real pod/event JSON captured from a kind cluster, plus fake API clients."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

from pfa.kube import KubeClients

FIXTURES = Path(__file__).parent / "fixtures"
_api = ApiClient()


@pytest.fixture(autouse=True)
def isolated_config(request, monkeypatch, tmp_path):
    """No test reads the developer's real config file or PFA_* / API key variables.

    Except `llm` tests (make test-llm): they exist to use the real provider settings.
    """
    import os

    if request.node.get_closest_marker("llm"):
        return
    for var in list(os.environ):
        if var.startswith("PFA_") or var == "ANTHROPIC_API_KEY":
            monkeypatch.delenv(var)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def to_k8s(data: dict[str, Any], klass: str):
    """Turn raw API JSON into the typed object the kubernetes client would return."""
    return _api.deserialize(json.dumps(data), klass, "application/json")


def pod(name: str, mutate=None):
    data = copy.deepcopy(load_json(f"pod_{name}.json"))
    if mutate:
        mutate(data)
    return to_k8s(data, "V1Pod")


def not_found() -> ApiException:
    return ApiException(status=404, reason="Not Found")


class FakeCore:
    def __init__(self, pods=(), events=(), logs=None, pvcs=()):
        self.pods = {p.metadata.name: p for p in pods}
        self.events = list(events)
        # (pod, previous) or (pod, container, previous) -> str | ApiException
        self.logs = logs or {}
        self.pvcs = pvcs if isinstance(pvcs, dict) else {c.metadata.name: c for c in pvcs}

    def read_namespaced_pod(self, name, namespace, **_):
        if name not in self.pods:
            raise not_found()
        return self.pods[name]

    def list_namespaced_pod(self, namespace, label_selector="", **_):
        wanted = dict(kv.split("=") for kv in label_selector.split(",") if "=" in kv)
        items = [
            p
            for p in self.pods.values()
            if all((p.metadata.labels or {}).get(k) == v for k, v in wanted.items())
        ]
        return SimpleNamespace(items=items)

    def list_namespaced_event(self, namespace, field_selector="", **_):
        wanted = dict(kv.split("=", 1) for kv in field_selector.split(",") if "=" in kv)
        return SimpleNamespace(
            items=[
                e
                for e in self.events
                if e.involved_object.name == wanted.get("involvedObject.name")
                and wanted.get("involvedObject.kind", e.involved_object.kind)
                == e.involved_object.kind
            ]
        )

    def read_namespaced_persistent_volume_claim(self, name, namespace, **_):
        if isinstance(self.pvcs.get(name), Exception):
            raise self.pvcs[name]
        if name not in self.pvcs:
            raise not_found()
        return self.pvcs[name]

    def read_namespaced_pod_log(self, name, namespace, container=None, previous=False, **_):
        result = self.logs.get((name, container, previous), self.logs.get((name, previous), ""))
        if isinstance(result, Exception):
            raise result
        return result


class FakeApps:
    def __init__(self, deployments=(), replicasets=(), statefulsets=()):
        self.deployments = {d.metadata.name: d for d in deployments}
        self.replicasets = {r.metadata.name: r for r in replicasets}
        self.statefulsets = {s.metadata.name: s for s in statefulsets}

    def read_namespaced_deployment(self, name, namespace, **_):
        if name not in self.deployments:
            raise not_found()
        return self.deployments[name]

    def read_namespaced_stateful_set(self, name, namespace, **_):
        if name not in self.statefulsets:
            raise not_found()
        return self.statefulsets[name]

    def read_namespaced_replica_set(self, name, namespace, **_):
        if name not in self.replicasets:
            raise not_found()
        return self.replicasets[name]

    def list_namespaced_replica_set(self, namespace, **_):
        return SimpleNamespace(items=list(self.replicasets.values()))


def make_clients(core=None, apps=None) -> KubeClients:
    return KubeClients(core=core or FakeCore(), apps=apps or FakeApps(), timeout=1.0)


def deployment(name: str, uid: str, labels: dict[str, str]):
    return to_k8s(
        {
            "metadata": {"name": name, "namespace": "default", "uid": uid, "generation": 1},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels}, "spec": {"containers": []}},
            },
            "status": {"observedGeneration": 1},
        },
        "V1Deployment",
    )


def replicaset(name: str, uid: str, owner_uid: str, owner_name: str, revision: int):
    return to_k8s(
        {
            "metadata": {
                "name": name,
                "namespace": "default",
                "uid": uid,
                "annotations": {"deployment.kubernetes.io/revision": str(revision)},
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": owner_name,
                        "uid": owner_uid,
                        "controller": True,
                    }
                ],
            },
            "spec": {"selector": {"matchLabels": {}}},
        },
        "V1ReplicaSet",
    )


@pytest.fixture
def oom_pod():
    return pod("oomkilled")


# --- synthetic evidence for classifier and rule tests ---------------------------------------


def make_view(
    container: dict | None = None,
    pod: dict | None = None,
    events: list[dict] | None = None,
    logs: str = "",
    kind: str = "deployment",
    has_pod: bool = True,
    claims: list[dict] | None = None,
):
    """An EvidenceView from hand-written summaries; only fields a test cares about are needed."""
    from pfa.models import Evidence, OwnerRef, Target, WorkloadKind
    from pfa.view import EvidenceView

    c = {"name": "app", "image": "registry.example.com/app:1.0", **(container or {})}
    pod_summary = {"name": "app-123", "phase": "Running", "containers": [c], **(pod or {})}
    target = Target(
        namespace="default",
        name="app",
        kind=WorkloadKind(kind),
        pod="app-123" if has_pod else None,
        container="app" if has_pod else None,
        owners=[OwnerRef(kind="Deployment", name="app")] if kind == "deployment" else [],
    )
    evs = [{"type": "Warning", "count": 1, "object": "Pod/app-123", **e} for e in (events or [])]
    evidence = [
        Evidence(
            source="k8s_state",
            summary={
                "pod": pod_summary if has_pod else None,
                **({"claims": claims} if claims else {}),
            },
        ),
        Evidence(source="k8s_events", summary={"events": evs}),
        Evidence(source="logs", summary={}, raw_excerpt=logs or None),
    ]
    return EvidenceView(target, evidence)


def waiting(reason: str, message: str = "") -> dict:
    return {"state": {"waiting": {"reason": reason, "message": message}}, "ready": False}


def crashed(exit_code: int, reason: str = "Error", restarts: int = 3, message: str = "") -> dict:
    term = {"reason": reason, "exit_code": exit_code}
    if message:
        term["message"] = message
    return {
        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        "last_state": {"terminated": term},
        "restart_count": restarts,
        "ready": False,
    }


# --- AI layer -----------------------------------------------------------------------------

EVIDENCE_FIXTURES = FIXTURES / "evidence"


def replay_report(failure_type: str):
    """The rules-only Report for one tests/fixtures/evidence/<failure_type>.json."""
    from pfa.engine.orchestrator import analyze
    from pfa.models import Evidence, Target

    data = json.loads((EVIDENCE_FIXTURES / f"{failure_type}.json").read_text())
    target = Target.model_validate(data["target"])
    return analyze(target, [Evidence.model_validate(e) for e in data["evidence"]])


class FakeProvider:
    """An LLMProvider that returns canned replies (str) or raises them (Exception)."""

    name = "fake"
    model = "fake-model"
    destination = "fake.example"
    local = False

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system, user, schema):
        self.calls.append({"system": system, "user": user, "schema": schema})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def llm_reply(**overrides) -> str:
    """A valid model reply for the crash-loop fixture; override any field."""
    reply = {
        "failure_type": "crash_loop_backoff",
        "problem": "checkout-api exits right after starting and is restarted over and over.",
        "likely_cause": "The app needs DATABASE_URL, which the Deployment does not set: "
        '"FATAL: required environment variable DATABASE_URL is not set".',
        "fix": ["Add DATABASE_URL to the container env (from a Secret) and roll out again."],
        "confidence": "high",
        "evidence_used": ["log: FATAL: required environment variable DATABASE_URL is not set"],
    }
    reply.update(overrides)
    return json.dumps(reply)
