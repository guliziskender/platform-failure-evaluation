"""Collector protocol and the safety wrapper every collector runs under."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from pfa.models import Evidence, FailureType, Target


class Collector(Protocol):
    name: str

    def applies_to(self, failure: FailureType) -> bool: ...

    def collect(self, target: Target) -> Evidence: ...


def run_safely(collector: Collector, target: Target) -> Evidence:
    """Run a collector; turn any failure into an Evidence with an error note.

    One broken data source must never fail the whole diagnosis.
    """
    try:
        return collector.collect(target)
    except ApiException as exc:
        return Evidence(source=collector.name, error=f"Kubernetes API {exc.status}: {exc.reason}")
    except HTTPError as exc:  # timeouts, connection refused
        return Evidence(source=collector.name, error=f"Kubernetes API unreachable: {exc}")
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all, see docstring
        return Evidence(source=collector.name, error=f"{type(exc).__name__}: {exc}")


def iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def drop_none(d: dict) -> dict:
    """Keep summaries compact: the LLM pays for every key."""
    return {k: v for k, v in d.items() if v not in (None, [], {})}
