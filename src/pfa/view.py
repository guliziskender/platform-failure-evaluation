"""Read-only accessors over collected evidence, shared by the classifier and the rules.

Works purely on Evidence summaries (not live API objects), so saved evidence can be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property
from typing import Any

from pfa.models import Evidence, Target

# A failure this recent still counts while the container is up again (matches the event window).
RECENT = timedelta(hours=1)


@dataclass
class EvidenceView:
    target: Target
    evidence: list[Evidence]

    def summary(self, source: str) -> dict[str, Any]:
        for ev in self.evidence:
            if ev.source == source and not ev.error:
                return ev.summary
        return {}

    def excerpt(self, source: str) -> str:
        for ev in self.evidence:
            if ev.source == source:
                return ev.raw_excerpt or ""
        return ""

    # --- pod and workload ---------------------------------------------------------------

    @cached_property
    def pod(self) -> dict[str, Any] | None:
        return self.summary("k8s_state").get("pod")

    @cached_property
    def workload(self) -> dict[str, Any] | None:
        return self.summary("k8s_state").get("workload")

    @cached_property
    def container(self) -> dict[str, Any]:
        """The target container's summary (init containers included); {} when there is none."""
        if not self.pod:
            return {}
        all_containers = self.pod.get("init_containers", []) + self.pod.get("containers", [])
        for c in all_containers:
            if c["name"] == self.target.container:
                return c
        return all_containers[0] if all_containers else {}

    @property
    def container_name(self) -> str:
        return self.container.get("name", "?")

    @property
    def is_init_container(self) -> bool:
        names = {c["name"] for c in (self.pod or {}).get("init_containers", [])}
        return self.container_name in names

    @property
    def container_label(self) -> str:
        """'container api' or 'init container migrate', for messages."""
        kind = "init container" if self.is_init_container else "container"
        return f"{kind} {self.container_name}"

    def condition(self, type_: str) -> dict[str, Any] | None:
        for c in (self.pod or {}).get("conditions", []):
            if c["type"] == type_:
                return c
        return None

    # --- container state ----------------------------------------------------------------

    @property
    def waiting(self) -> dict[str, Any]:
        return (self.container.get("state") or {}).get("waiting") or {}

    @property
    def terminated(self) -> dict[str, Any]:
        return (self.container.get("state") or {}).get("terminated") or {}

    @property
    def last_terminated(self) -> dict[str, Any]:
        return (self.container.get("last_state") or {}).get("terminated") or {}

    @property
    def restarts(self) -> int:
        return self.container.get("restart_count", 0)

    def termination(self) -> dict[str, Any]:
        """The most informative termination: current if the container is dead now, else last."""
        return self.terminated or self.last_terminated

    @cached_property
    def now(self) -> datetime | None:
        """When the pod state was collected. Used instead of the wall clock, so replays of
        saved evidence give the same answer as the live run did."""
        for ev in self.evidence:
            if ev.source == "k8s_state":
                return ev.collected_at
        return None

    def failed_recently(self) -> bool:
        """Is the container still failing, even if it happens to be up right now?

        True if it last exited abnormally within RECENT of collection, or the kubelet is
        backing off restarts. A crash-looping app spends part of every cycle running and
        ready; without this it would look healthy in that window.
        """
        if self.events_where("BackOff", "restarting failed container"):
            return True
        term = self.termination()
        abnormal = term.get("exit_code", 0) != 0 or term.get("reason") == "OOMKilled"
        finished = _parse_time(term.get("finished_at"))
        if not abnormal or finished is None or self.now is None:
            return False
        return self.now - finished <= RECENT

    # --- events and logs ----------------------------------------------------------------

    @cached_property
    def events(self) -> list[dict[str, Any]]:
        return self.summary("k8s_events").get("events", [])

    def events_where(self, reason: str | None = None, contains: str = "") -> list[dict[str, Any]]:
        return [
            e
            for e in self.events
            if (reason is None or e.get("reason") == reason) and contains in e.get("message", "")
        ]

    @property
    def logs(self) -> str:
        return self.excerpt("logs")


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
