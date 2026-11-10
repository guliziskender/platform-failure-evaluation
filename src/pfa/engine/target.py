"""Resolve a user-supplied name to a Target: which pod and container to diagnose."""

from __future__ import annotations

from datetime import UTC, datetime

from kubernetes.client import V1ContainerStatus, V1LabelSelector, V1Pod, V1ReplicaSet
from kubernetes.client.exceptions import ApiException

from pfa.kube import KubeClients
from pfa.models import OwnerRef, Target, WorkloadKind

_EPOCH = datetime.min.replace(tzinfo=UTC)
# Waiting reasons that just mean "an earlier step hasn't finished", not a failure of this container.
_BENIGN_WAITING = {"PodInitializing"}


class TargetNotFound(LookupError):
    pass


def resolve_target(
    clients: KubeClients, name: str, namespace: str, kind: WorkloadKind | None = None
) -> Target:
    """Find `name` as a pod, Deployment or StatefulSet (in that order unless `kind` is given)."""
    for candidate in [kind] if kind else list(WorkloadKind):
        try:
            if candidate is WorkloadKind.POD:
                return _resolve_pod(clients, name, namespace)
            if candidate is WorkloadKind.DEPLOYMENT:
                return _resolve_deployment(clients, name, namespace)
            if candidate is WorkloadKind.STATEFULSET:
                return _resolve_statefulset(clients, name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
    looked_for = kind.value if kind else "pod, deployment or statefulset"
    raise TargetNotFound(f"no {looked_for} named {name!r} in namespace {namespace!r}")


def _resolve_pod(clients: KubeClients, name: str, namespace: str) -> Target:
    pod = clients.core.read_namespaced_pod(name, namespace, _request_timeout=clients.timeout)
    return Target(
        namespace=namespace,
        name=name,
        kind=WorkloadKind.POD,
        pod=name,
        container=pick_container(pod),
        owners=_owner_chain(clients, pod, namespace),
    )


def _resolve_deployment(clients: KubeClients, name: str, namespace: str) -> Target:
    deploy = clients.apps.read_namespaced_deployment(
        name, namespace, _request_timeout=clients.timeout
    )
    selector = label_selector(deploy.spec.selector)
    replicasets = [
        rs
        for rs in clients.apps.list_namespaced_replica_set(
            namespace, label_selector=selector, _request_timeout=clients.timeout
        ).items
        if _controller_uid(rs) == deploy.metadata.uid
    ]
    rs_uids = {rs.metadata.uid for rs in replicasets}
    pods = [p for p in _list_pods(clients, namespace, selector) if _controller_uid(p) in rs_uids]
    pod = pick_pod(pods)

    owners = [OwnerRef(kind="Deployment", name=name)]
    rs_name = _controller_name(pod) if pod else None
    if rs_name is None and replicasets:
        # No pods at all: the newest ReplicaSet carries events like FailedCreate (quota, admission).
        rs_name = max(replicasets, key=_revision).metadata.name
    if rs_name:
        owners.insert(0, OwnerRef(kind="ReplicaSet", name=rs_name))
    return _workload_target(name, namespace, WorkloadKind.DEPLOYMENT, pod, owners)


def _resolve_statefulset(clients: KubeClients, name: str, namespace: str) -> Target:
    sts = clients.apps.read_namespaced_stateful_set(
        name, namespace, _request_timeout=clients.timeout
    )
    selector = label_selector(sts.spec.selector)
    pods = [
        p
        for p in _list_pods(clients, namespace, selector)
        if _controller_uid(p) == sts.metadata.uid
    ]
    owners = [OwnerRef(kind="StatefulSet", name=name)]
    return _workload_target(name, namespace, WorkloadKind.STATEFULSET, pick_pod(pods), owners)


def _workload_target(
    name: str, namespace: str, kind: WorkloadKind, pod: V1Pod | None, owners: list[OwnerRef]
) -> Target:
    return Target(
        namespace=namespace,
        name=name,
        kind=kind,
        pod=pod.metadata.name if pod else None,
        container=pick_container(pod) if pod else None,
        owners=owners,
    )


def _list_pods(clients: KubeClients, namespace: str, selector: str) -> list[V1Pod]:
    return clients.core.list_namespaced_pod(
        namespace, label_selector=selector, _request_timeout=clients.timeout
    ).items


def _owner_chain(clients: KubeClients, pod: V1Pod, namespace: str) -> list[OwnerRef]:
    """pod -> ReplicaSet -> Deployment, or pod -> StatefulSet. Best effort: stops on API errors."""
    owners: list[OwnerRef] = []
    ref = _controller_ref(pod)
    if ref is None:
        return owners
    owners.append(OwnerRef(kind=ref.kind, name=ref.name))
    if ref.kind == "ReplicaSet":
        try:
            rs = clients.apps.read_namespaced_replica_set(
                ref.name, namespace, _request_timeout=clients.timeout
            )
        except ApiException:
            return owners
        parent = _controller_ref(rs)
        if parent is not None:
            owners.append(OwnerRef(kind=parent.kind, name=parent.name))
    return owners


# --- picking the most broken pod and container -------------------------------------------


def pick_pod(pods: list[V1Pod]) -> V1Pod | None:
    """The most recently failing pod; ties go to the most restarts, then the newest pod.

    Terminating pods come last: right after a fix is rolled out, the old crashing pod lingers
    while it shuts down, and diagnosing it would report a problem that is already gone.
    """
    return max(pods, key=_pod_rank, default=None)


def pick_container(pod: V1Pod) -> str | None:
    statuses = _all_statuses(pod)
    broken = [cs for cs in statuses if _is_broken(cs)]
    if broken:
        return max(broken, key=_container_rank).name
    containers = pod.spec.containers or []
    return containers[0].name if containers else None


def _pod_rank(pod: V1Pod) -> tuple:
    statuses = _all_statuses(pod)
    unhealthy = pod.status.phase in ("Pending", "Failed", "Unknown") or any(
        _is_broken(cs) for cs in statuses
    )
    last_failure = max((_last_failure(cs) for cs in statuses), default=_EPOCH)
    restarts = sum(cs.restart_count or 0 for cs in statuses)
    created = pod.metadata.creation_timestamp or _EPOCH
    staying = pod.metadata.deletion_timestamp is None
    return (staying, unhealthy, last_failure, restarts, created)


def _container_rank(cs: V1ContainerStatus) -> tuple:
    return (_last_failure(cs), cs.restart_count or 0)


def _is_broken(cs: V1ContainerStatus) -> bool:
    state = cs.state
    if state is None:
        return False
    if state.waiting and state.waiting.reason not in _BENIGN_WAITING:
        return True
    if state.terminated and (state.terminated.exit_code or 0) != 0:
        return True
    if state.running and not cs.ready:
        return True  # e.g. readiness probe failing
    return (cs.restart_count or 0) > 0


def _last_failure(cs: V1ContainerStatus) -> datetime:
    times = [
        st.terminated.finished_at
        for st in (cs.state, cs.last_state)
        if st
        and st.terminated
        and (st.terminated.exit_code or 0) != 0
        and st.terminated.finished_at
    ]
    return max(times, default=_EPOCH)


def _all_statuses(pod: V1Pod) -> list[V1ContainerStatus]:
    status = pod.status
    if status is None:
        return []
    return (status.init_container_statuses or []) + (status.container_statuses or [])


# --- small helpers ---------------------------------------------------------------------------


def label_selector(selector: V1LabelSelector) -> str:
    parts = [f"{k}={v}" for k, v in (selector.match_labels or {}).items()]
    for expr in selector.match_expressions or []:
        values = ",".join(expr.values or [])
        match expr.operator:
            case "In":
                parts.append(f"{expr.key} in ({values})")
            case "NotIn":
                parts.append(f"{expr.key} notin ({values})")
            case "Exists":
                parts.append(expr.key)
            case "DoesNotExist":
                parts.append(f"!{expr.key}")
    return ",".join(parts)


def _controller_ref(obj):
    return next((r for r in obj.metadata.owner_references or [] if r.controller), None)


def _controller_uid(obj) -> str | None:
    ref = _controller_ref(obj)
    return ref.uid if ref else None


def _controller_name(obj) -> str | None:
    ref = _controller_ref(obj)
    return ref.name if ref else None


def _revision(rs: V1ReplicaSet) -> int:
    annotations = rs.metadata.annotations or {}
    try:
        return int(annotations.get("deployment.kubernetes.io/revision", 0))
    except ValueError:
        return 0
