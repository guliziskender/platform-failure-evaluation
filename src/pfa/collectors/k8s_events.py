"""Events for the pod and its owners, deduplicated, newest first.

Owner events (ReplicaSet, Deployment) older than the window are dropped: they may be about an
earlier problem. The pod's own events are kept whatever their age, because the kubelet stops
updating them during a long failure. On a pod crash-looping for hours, the "Liveness probe
failed" event was last updated 90 minutes before the BackOff event, and a 60-minute window had
hidden the cause.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from kubernetes.client import CoreV1Event

from pfa.collectors.base import drop_none, iso
from pfa.kube import KubeClients
from pfa.models import Evidence, FailureType, Target

MAX_EVENTS = 30
# Numbers vary between repeats of one event ("pulled in 813ms", "Image size: 4041495 bytes").
_NUMBERS = re.compile(r"\b\d+(\.\d+)?(ms|s|m|h)?\b")


class K8sEventsCollector:
    name = "k8s_events"

    def __init__(self, clients: KubeClients, window_minutes: int = 60):
        self.clients = clients
        self.window = timedelta(minutes=window_minutes)

    def applies_to(self, failure: FailureType) -> bool:
        return failure is not FailureType.DOCKER_BUILD

    def collect(self, target: Target) -> Evidence:
        objects = [("Pod", target.pod)] if target.pod else []
        objects += [(o.kind, o.name) for o in target.owners]

        raw: list[CoreV1Event] = []
        for kind, name in objects:
            raw += [
                e
                for e in self.clients.core.list_namespaced_event(
                    target.namespace,
                    field_selector=f"involvedObject.name={name}",
                    _request_timeout=self.clients.timeout,
                ).items
                if e.involved_object.kind == kind
            ]

        pod = f"Pod/{target.pod}" if target.pod else None
        events = dedupe_events(raw, since=datetime.now(UTC) - self.window, keep=pod)
        return Evidence(
            source=self.name,
            summary={
                "window_minutes": int(self.window.total_seconds() // 60),
                "objects": [f"{k}/{n}" for k, n in objects],
                "total": len(events),
                "events": events[:MAX_EVENTS],
            },
        )


def dedupe_events(
    events: list[CoreV1Event], since: datetime, keep: str | None = None
) -> list[dict[str, Any]]:
    """Merge repeats of the same (object, reason, message); drop events older than `since`,
    except those about object `keep` (the target pod, "Pod/<name>")."""
    merged: dict[tuple, dict[str, Any]] = {}
    for e in events:
        last = _last_seen(e)
        obj = f"{e.involved_object.kind}/{e.involved_object.name}"
        if last is None or (last < since and obj != keep):
            continue
        key = (obj, e.reason, _NUMBERS.sub("#", e.message or ""))
        count = _count(e)
        first = e.first_timestamp or e.event_time or last
        if key in merged:
            m = merged[key]
            m["count"] += count
            m["_first"] = min(m["_first"], first)
            if last > m["_last"]:
                m["_last"], m["message"] = last, (e.message or "").strip()
        else:
            merged[key] = {
                "type": e.type,
                "reason": e.reason,
                "object": obj,
                "message": (e.message or "").strip(),
                "count": count,
                "_first": first,
                "_last": last,
            }
    # Warnings first, then newest first: the cap should cut Normal noise, not the signal.
    ordered = sorted(
        merged.values(), key=lambda m: (m["type"] == "Warning", m["_last"]), reverse=True
    )
    return [
        drop_none(
            {
                **{k: v for k, v in m.items() if not k.startswith("_")},
                "first_seen": iso(m["_first"]),
                "last_seen": iso(m["_last"]),
            }
        )
        for m in ordered
    ]


def _last_seen(e: CoreV1Event) -> datetime | None:
    if e.series and e.series.last_observed_time:
        return e.series.last_observed_time
    return e.last_timestamp or e.event_time or e.metadata.creation_timestamp


def _count(e: CoreV1Event) -> int:
    if e.series and e.series.count:
        return e.series.count
    return e.count or 1
