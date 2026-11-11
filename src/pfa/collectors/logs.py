"""Current and previous container logs, trimmed to the tail."""

from __future__ import annotations

import json
from typing import Any

from kubernetes.client.exceptions import ApiException

from pfa.collectors.base import drop_none
from pfa.kube import KubeClients
from pfa.models import Evidence, FailureType, Target

_KUBELET_UNAVAILABLE = "unable to retrieve container logs"


class LogsCollector:
    name = "logs"

    def __init__(self, clients: KubeClients, tail_lines: int = 200, max_bytes: int = 4096):
        self.clients = clients
        self.tail_lines = tail_lines
        self.max_bytes = max_bytes

    def applies_to(self, failure: FailureType) -> bool:
        return failure not in (
            FailureType.DOCKER_BUILD,
            FailureType.PENDING,
            FailureType.IMAGE_PULL,
        )

    def collect(self, target: Target) -> Evidence:
        if target.pod is None or target.container is None:
            return Evidence(
                source=self.name, summary={"note": "no pod or container to read logs from"}
            )

        summary: dict[str, Any] = {"container": target.container}
        sections: list[str] = []
        # The target container's sections keep the plain header, so older saved evidence reads
        # the same. Other failing containers get theirs labelled: "=== worker: previous ...".
        for name in [target.container, *target.also_failing]:
            info: dict[str, Any] = {}
            prefix = "" if name == target.container else f"{name}: "
            # Previous logs first: for crash loops they hold the actual crash.
            for label, previous in (("previous", True), ("current", False)):
                info[label], text = self._read(target, name, previous)
                if text:
                    sections.append(f"=== {prefix}{label} container ===\n{text}")
            if name == target.container:
                summary.update(info)
            else:
                summary.setdefault("others", {})[name] = info
        return Evidence(
            source=self.name,
            summary=summary,
            raw_excerpt="\n".join(sections) or None,
        )

    def _read(self, target: Target, container: str, previous: bool) -> tuple[dict[str, Any], str]:
        try:
            text = self.clients.core.read_namespaced_pod_log(
                target.pod,
                target.namespace,
                container=container,
                previous=previous,
                tail_lines=self.tail_lines,
                _request_timeout=self.clients.timeout,
            )
        except ApiException as exc:
            # 400 = no previous instance, or container not started yet. Expected, not an error.
            if exc.status == 400:
                return {"available": False, "note": _api_message(exc)}, ""
            raise
        text = text or ""
        if text.startswith(_KUBELET_UNAVAILABLE):
            # Kubelet answers 200 with this text once the old container was garbage-collected.
            return {"available": False, "note": text.strip().splitlines()[0]}, ""
        trimmed, truncated = trim_tail(text, self.max_bytes)
        return drop_none(
            {
                "available": True,
                "lines": len(trimmed.splitlines()),
                "truncated": truncated or None,
                "empty": (not text.strip()) or None,
            }
        ), trimmed


def trim_tail(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the last `max_bytes` of text, cut at a line boundary."""
    data = text.encode()
    if len(data) <= max_bytes:
        return text, False
    tail = data[-max_bytes:].decode(errors="ignore")
    newline = tail.find("\n")
    if 0 <= newline < len(tail) - 1:
        tail = tail[newline + 1 :]
    return tail, True


def _api_message(exc: ApiException) -> str:
    try:
        return json.loads(exc.body).get("message", exc.reason)
    except (TypeError, ValueError):
        return str(exc.reason)
