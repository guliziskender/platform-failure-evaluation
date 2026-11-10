"""Shared data models: what we diagnose (Target) and what collectors return (Evidence)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FailureType(StrEnum):
    CRASH_LOOP = "crash_loop_backoff"
    OOM_KILLED = "oom_killed"
    IMAGE_PULL = "image_pull_backoff"
    PENDING = "pending"
    PROBE_FAILURE = "probe_failure"
    DOCKER_BUILD = "docker_build"
    UNKNOWN = "unknown"  # something is wrong, but not one of the types above
    NONE = "none"  # no failure detected


class WorkloadKind(StrEnum):
    POD = "pod"
    DEPLOYMENT = "deployment"
    STATEFULSET = "statefulset"


class OwnerRef(BaseModel):
    kind: str
    name: str


class Target(BaseModel):
    """A resolved diagnosis target: the workload the user named and the pod we picked."""

    namespace: str
    name: str
    kind: WorkloadKind
    pod: str | None = None  # None when a workload has no pods at all (e.g. quota blocks creation)
    container: str | None = None  # the container that looks most broken
    owners: list[OwnerRef] = Field(default_factory=list)  # pod -> ReplicaSet -> Deployment chain


class Evidence(BaseModel):
    """Structured facts from one source. Collectors never raise; they set `error` instead."""

    source: str
    summary: dict[str, Any] = Field(default_factory=dict)
    raw_excerpt: str | None = None
    error: str | None = None
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Set by pfa.ai.redact. Evidence must be redacted before anything reads or prints it.
    redacted: bool = False
    redactions: dict[str, int] = Field(default_factory=dict)  # placeholders by kind


Certainty = Literal["high", "medium", "low"]
# "unsure" = no cause identified; shown instead of guessing.
Confidence = Literal["high", "medium", "low", "unsure"]


class Classification(BaseModel):
    """What kind of failure this is, decided deterministically from pod state and events."""

    failure_type: FailureType
    problem: str  # one sentence: what is happening
    signals: list[str] = Field(default_factory=list)  # facts the classification is based on


class RuleHit(BaseModel):
    """A deterministic finding: the answer under --no-llm, a hint for the LLM later."""

    rule: str  # rule id, e.g. "oom_memory_limit"
    failure_type: FailureType
    finding: str  # what the rule observed
    cause: str  # why it is happening
    fix: list[str]  # concrete steps, most likely first
    certainty: Certainty
    evidence: list[str] = Field(default_factory=list)  # signals the hit is based on


class Diagnosis(BaseModel):
    failure_type: FailureType
    problem: str
    likely_cause: str
    fix: list[str]
    confidence: Confidence
    evidence_used: list[str] = Field(default_factory=list)
    rule_hits: list[str] = Field(default_factory=list)
    source: Literal["rules", "llm"] = "rules"
    model: str | None = None  # which model answered, when source is "llm"


REPORT_SCHEMA_VERSION = 1  # bump on breaking changes to `pfa diagnose -o json`


class Report(BaseModel):
    """Everything one `pfa diagnose` run produced."""

    schema_version: int = REPORT_SCHEMA_VERSION
    target: Target
    classification: Classification
    rule_hits: list[RuleHit]
    diagnosis: Diagnosis
    evidence: list[Evidence]
    # Things the user should know about how this answer was produced, e.g. why the AI was not
    # used, or that the AI and the rules disagree.
    notes: list[str] = Field(default_factory=list)
