"""Pod and workload state: phase, container statuses, exit codes, resources, probes."""

from __future__ import annotations

from typing import Any

from kubernetes.client import (
    V1Container,
    V1ContainerState,
    V1ContainerStatus,
    V1Pod,
    V1Probe,
)

from pfa.collectors.base import drop_none, iso
from pfa.kube import KubeClients
from pfa.models import Evidence, FailureType, Target, WorkloadKind


class K8sStateCollector:
    name = "k8s_state"

    def __init__(self, clients: KubeClients):
        self.clients = clients

    def applies_to(self, failure: FailureType) -> bool:
        return failure is not FailureType.DOCKER_BUILD

    def collect(self, target: Target) -> Evidence:
        summary: dict[str, Any] = {}
        if target.kind is not WorkloadKind.POD:
            summary["workload"] = self._workload(target)
        if target.pod is None:
            summary["pod"] = None
            summary["note"] = "workload has no pods"
        else:
            pod = self.clients.core.read_namespaced_pod(
                target.pod, target.namespace, _request_timeout=self.clients.timeout
            )
            summary["pod"] = summarize_pod(pod)
        return Evidence(source=self.name, summary=summary)

    def _workload(self, target: Target) -> dict[str, Any]:
        apps, t = self.clients.apps, self.clients.timeout
        if target.kind is WorkloadKind.DEPLOYMENT:
            obj = apps.read_namespaced_deployment(target.name, target.namespace, _request_timeout=t)
        else:
            obj = apps.read_namespaced_stateful_set(
                target.name, target.namespace, _request_timeout=t
            )
        st = obj.status
        return drop_none(
            {
                "kind": target.kind.value,
                "name": target.name,
                "replicas_desired": obj.spec.replicas,
                "replicas_ready": st.ready_replicas or 0,
                "replicas_updated": st.updated_replicas or 0,
                "replicas_available": getattr(st, "available_replicas", None) or 0,
                "generation": obj.metadata.generation,
                "observed_generation": st.observed_generation,
                "conditions": [
                    drop_none(
                        {
                            "type": c.type,
                            "status": c.status,
                            "reason": c.reason,
                            "message": c.message,
                            "last_update": iso(getattr(c, "last_update_time", None)),
                        }
                    )
                    for c in st.conditions or []
                ],
            }
        )


def summarize_pod(pod: V1Pod) -> dict[str, Any]:
    spec, status = pod.spec, pod.status
    statuses = {
        cs.name: cs
        for cs in (status.init_container_statuses or []) + (status.container_statuses or [])
    }
    return drop_none(
        {
            "name": pod.metadata.name,
            "phase": status.phase,
            "reason": status.reason,
            "message": status.message,
            "node": spec.node_name,
            "qos_class": status.qos_class,
            "created": iso(pod.metadata.creation_timestamp),
            "started": iso(status.start_time),
            "conditions": [
                drop_none(
                    {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                )
                for c in status.conditions or []
            ],
            "init_containers": [
                summarize_container(c, statuses.get(c.name)) for c in spec.init_containers or []
            ],
            "containers": [summarize_container(c, statuses.get(c.name)) for c in spec.containers],
            "scheduling": drop_none(
                {
                    "node_selector": spec.node_selector,
                    "tolerations": [
                        drop_none({"key": t.key, "operator": t.operator, "effect": t.effect})
                        for t in spec.tolerations or []
                        # Every pod gets these two by default; they are noise.
                        if t.key
                        not in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")
                    ],
                    "has_affinity": bool(spec.affinity) or None,
                    "priority_class": spec.priority_class_name,
                }
            ),
            "pvcs": [
                v.persistent_volume_claim.claim_name
                for v in spec.volumes or []
                if v.persistent_volume_claim
            ],
            "image_pull_secrets": [s.name for s in spec.image_pull_secrets or []],
        }
    )


def summarize_container(c: V1Container, cs: V1ContainerStatus | None) -> dict[str, Any]:
    resources = c.resources
    out: dict[str, Any] = {
        "name": c.name,
        "image": c.image,
        "command": c.command,
        "args": c.args,
        "ports": [_port(p) for p in c.ports or []],
        "resources": drop_none(
            {
                "requests": resources.requests if resources else None,
                "limits": resources.limits if resources else None,
            }
        ),
        "probes": drop_none(
            {
                "startup": _probe(c.startup_probe),
                "liveness": _probe(c.liveness_probe),
                "readiness": _probe(c.readiness_probe),
            }
        ),
        # Names only, never values: values are where secrets live.
        "env_names": [e.name for e in c.env or []],
        "env_from": [_env_from(e) for e in c.env_from or []],
    }
    if cs is not None:
        out.update(
            {
                "ready": cs.ready,
                "started": cs.started,
                "restart_count": cs.restart_count,
                "state": _state(cs.state),
                "last_state": _state(cs.last_state),
                "image_id": cs.image_id or None,
            }
        )
    return drop_none(out)


def _state(state: V1ContainerState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    if state.waiting:
        return {
            "waiting": drop_none({"reason": state.waiting.reason, "message": state.waiting.message})
        }
    if state.terminated:
        t = state.terminated
        return {
            "terminated": drop_none(
                {
                    "reason": t.reason,
                    "exit_code": t.exit_code,
                    "signal": t.signal,
                    "message": t.message,
                    "started_at": iso(t.started_at),
                    "finished_at": iso(t.finished_at),
                }
            )
        }
    if state.running:
        return {"running": drop_none({"started_at": iso(state.running.started_at)})}
    return None


def _probe(p: V1Probe | None) -> dict[str, Any] | None:
    if p is None:
        return None
    if p.http_get:
        h = p.http_get
        action = {"http_get": f"{h.scheme or 'HTTP'} {h.path or '/'} port={h.port}"}
    elif p.tcp_socket:
        action = {"tcp_socket": f"port={p.tcp_socket.port}"}
    elif p.grpc:
        action = {"grpc": f"port={p.grpc.port}"}
    elif p._exec:
        action = {"exec": p._exec.command}
    else:
        action = {}
    return drop_none(
        {
            **action,
            "initial_delay_seconds": p.initial_delay_seconds,
            "period_seconds": p.period_seconds,
            "timeout_seconds": p.timeout_seconds,
            "failure_threshold": p.failure_threshold,
        }
    )


def _port(p) -> str:
    """'80/TCP', or 'http=80/TCP' for named ports (probes often refer to the name)."""
    port = f"{p.container_port}/{p.protocol or 'TCP'}"
    return f"{p.name}={port}" if p.name else port


def _env_from(e) -> str:
    if e.config_map_ref:
        return f"configmap/{e.config_map_ref.name}"
    if e.secret_ref:
        return f"secret/{e.secret_ref.name}"
    return "unknown"
