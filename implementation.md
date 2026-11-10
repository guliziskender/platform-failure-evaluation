# Platform Failure Assistant: Project & Implementation Plan

Oct 6, 2026 · @Zeynep güliz İskender

## Overview

Platform Failure Assistant (`pfa`) turns cryptic Kubernetes and Docker failures into a plain-language diagnosis with a concrete fix, so developers unblock themselves and the platform team gets its time back.

**The problem.** When a pod crashes or a deploy hangs, developers see only a symptom. Finding the cause means running several `kubectl` commands, reading events and logs, and comparing with the last working version. Many developers lack that knowledge, so they ask the platform team, which spends hours a week on repetitive questions.

**What the tool does.** On request (or automatically, later), it collects the same evidence an experienced platform engineer would, sends a cleaned and structured summary to an AI model, and returns the problem, the likely cause, a fix and a confidence level.

```
$ pfa diagnose payment-service
Problem:     Container OOMKilled 3 times in the last 10 minutes
Likely cause: New version (deployed 12 min ago) loads a larger cache;
              memory peaks at ~600Mi, limit is 512Mi
Fix:         Raise memory limit to 768Mi in deployment.yaml,
              or lower CACHE_MAX_SIZE
Confidence:  High (exit code 137 + memory metrics + timing matches deploy)
```

**Goals.**

- Developers resolve common failures without opening a platform-team ticket.
- The tool is safe to install in a cluster its author does not control: read-only, secrets redacted, no outbound data unless configured.
- Diagnosis accuracy is measured, not guessed, with a repeatable evaluation suite.
- The project doubles as a portfolio piece for Forward Deployed Engineer roles: integrations, safe deployment into unfamiliar environments, and measurable results.

## Scope for v1

Version 1 diagnoses six common Kubernetes and Docker failures well, through a CLI, and never changes anything in the cluster.

**In scope:** Kubernetes and Docker failure analysis, a CLI interface, read-only cluster access, local and hosted LLM backends.

**Out of scope for now:** application security, automatic remediation, multi-cluster views, a web UI.

| Failure | Main signals the tool reads | Typical root causes |
| --- | --- | --- |
| CrashLoopBackOff | Exit code, previous container logs, restart count | App error on startup, missing config or env var, bad command |
| OOMKilled | Exit code 137, `lastState.terminated.reason`, memory metrics | Memory limit too low, leak, larger workload after a deploy |
| ImagePullBackOff | Pod events, image name and tag, pull secrets | Wrong tag, private registry without credentials, registry down |
| Pending pod | Scheduler events, resource requests, node capacity, taints | Not enough CPU or memory, node selector or taint mismatch, unbound PVC |
| Failing probes | Probe config, events, container logs, start time | Probe path or port wrong, app slow to start, `initialDelaySeconds` too short |
| Docker build failure | Build log output, Dockerfile, failing step | Missing file in build context, dependency install error, base image change |

## Architecture

The system has four layers, and the AI sits in the middle: deterministic collectors gather evidence first, and the engine validates every answer after.

&#91;embedded content: architecture · 4 layers\]

A request flows top down: the engine gathers evidence, the AI layer redacts and diagnoses it, and the result is stored for evaluation. Every interface calls the same engine, so adding the watcher or Slack bot never touches the diagnosis logic.

## Tech stack and repository layout

This plan assumes Python, the fastest path to a working tool; the language choice is still open (see Open decisions).

| Concern | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 | Mature Kubernetes client, fast iteration with Claude Code |
| Kubernetes access | `kubernetes` official client | Reads pods, events, logs, ReplicaSets through the API |
| CLI | Typer + Rich | Typed commands, readable coloured output |
| Data models and LLM output | Pydantic v2 | Validates the model's JSON before anything is shown |
| HTTP | httpx | Prometheus queries and LLM APIs, with timeouts |
| LLM backends | Anthropic SDK, OpenAI SDK, Ollama (HTTP) | Hosted and fully local options behind one interface |
| Storage | SQLite via SQLModel, Postgres later | Zero setup locally, same models in-cluster |
| Tests | pytest, kind for integration tests | Real cluster behaviour in CI |
| Packaging | uv, Docker image, Helm chart | Reproducible installs locally and in a cluster |

```
platform-failure-assistant/
├── CLAUDE.md
├── pyproject.toml
├── src/pfa/
│   ├── cli.py              # Typer entry point: pfa diagnose, pfa eval
│   ├── config.py           # settings from env vars / config file
│   ├── engine/
│   │   ├── orchestrator.py # runs one diagnosis end to end
│   │   └── classifier.py   # detects the failure type from pod state
│   ├── collectors/
│   │   ├── base.py         # Collector protocol + Evidence model
│   │   ├── k8s_state.py    # pod, container status, spec, limits
│   │   ├── k8s_events.py
│   │   ├── logs.py
│   │   ├── metrics.py      # Prometheus (optional)
│   │   └── deploy_diff.py  # current vs previous ReplicaSet
│   ├── ai/
│   │   ├── redact.py
│   │   ├── context.py      # builds the compact prompt
│   │   ├── rules.py        # deterministic pre-checks
│   │   ├── prompts/        # one template per failure type
│   │   └── providers/      # base.py, anthropic.py, openai.py, ollama.py
│   ├── models.py           # Diagnosis, Evidence, Incident schemas
│   ├── render.py           # terminal and JSON output
│   └── store.py            # incident store
├── eval/
│   ├── cases/              # broken manifests + expected root cause
│   └── run_eval.py
├── deploy/helm/pfa/
└── tests/
```

## Implementation: evidence collectors

Collectors gather facts deterministically, with no AI, and all share one interface so a new data source is a new adapter, not a rewrite.

```python
class Evidence(BaseModel):
    source: str  # "k8s_state", "logs", "metrics", ...
    summary: dict  # structured facts, not raw dumps
    raw_excerpt: str | None = None  # trimmed text, e.g. last log lines
    collected_at: datetime


class Collector(Protocol):
    name: str

    def applies_to(self, failure: FailureType) -> bool: ...
    def collect(self, target: Target) -> Evidence: ...
```

| Collector | What it reads | Key details |
| --- | --- | --- |
| `k8s_state` | Pod phase, container statuses, `lastState.terminated` (exit code, reason), restart count, resource requests and limits, probes, image | Resolves a Deployment or StatefulSet name to its pods; picks the most recently failing pod |
| `k8s_events` | Events for the pod and its owner, last 1 hour | Deduplicate repeated events; keep reason, message, count, last timestamp |
| `logs` | Current and `previous=True` container logs | Last 200 lines, cut to about 4 KB; previous logs matter most for crash loops |
| `metrics` | Prometheus: memory working set and CPU per container, last 30 minutes | Optional; skipped cleanly when no Prometheus URL is configured |
| `deploy_diff` | Current vs previous ReplicaSet of the Deployment | Diff of image, env var names, resources, command; plus time since rollout |
| `docker_build` | Build log from a file or stdin | Finds the failing step and its error lines |

**Rules for every collector:**

- Return structured summaries first and raw text only as a trimmed excerpt; the LLM gets facts, not dumps.
- Never fail the whole diagnosis: on an API error or timeout (default 10 s), return an `Evidence` with an error note so the engine continues with what it has.
- Never read Secret objects or environment variable values, only their names.

## Implementation: AI layer

The AI layer turns evidence into a diagnosis in three steps: redact, pre-check with rules, then ask the model for structured JSON.

**1. Redaction (`ai/redact.py`).** Runs on every piece of text before it leaves the process.

- Regex patterns for common secrets: bearer tokens, AWS keys, JWTs, private key blocks, `password=`/`token=` style pairs, connection strings with credentials.
- Replace matches with typed placeholders such as `<REDACTED:jwt>` so the model still knows something was there.
- Unit-test it with a fixture file of fake secrets; redaction failures are the one bug this tool cannot ship.

**2. Rules pre-check (`ai/rules.py`).** Deterministic checks catch obvious cases instantly and cheaply.

- Example: exit code 137 and reason `OOMKilled` → failure type OOM, high certainty.
- Example: event reason `ErrImagePull` with message `not found` → wrong image tag.
- A rule returns a `RuleHit` (failure type, finding, certainty). It is passed to the model as a hint; with `--no-llm` the tool can answer from rules alone.

**3. Context builder and prompt (`ai/context.py`, `ai/prompts/`).** Builds a compact prompt from the evidence: one template per failure type, a fixed section order (status, events, logs excerpt, metrics, deploy diff, rule hints), and a token budget of about 6,000 tokens with log excerpts trimmed first.

**4. Provider adapter (`ai/providers/`).** One interface, several backends, chosen in config.

```python
class LLMProvider(Protocol):
    def diagnose(self, prompt: str, schema: type[Diagnosis]) -> Diagnosis: ...
```

- Anthropic and OpenAI adapters use the SDKs' structured or tool-based output; the Ollama adapter asks for JSON and parses it.
- Every call: 30 s timeout, up to 2 retries with backoff on rate limits, then a clear error.
- Output is parsed into the `Diagnosis` model. Invalid JSON gets one repair retry; if it still fails, the tool falls back to the rule hit or reports "unsure".
- The prompt instructs the model to cite which evidence supports the cause and to lower confidence when evidence is missing.

## Implementation: diagnosis engine, output schema and CLI

The engine coordinates one diagnosis end to end and contains no Kubernetes- or LLM-specific code.

**Engine steps (`engine/orchestrator.py`):**

1. Resolve the target: a pod, Deployment or StatefulSet name plus namespace.
2. Classify the failure from pod state (`engine/classifier.py`); unknown types still go through with generic evidence.
3. Run the collectors that apply to that failure type, in parallel with a thread pool.
4. Redact, run the rules, build the prompt, call the provider.
5. Validate the `Diagnosis`, render it, save it as an incident.

**Output schema (`models.py`):**

```python
class Diagnosis(BaseModel):
    failure_type: FailureType
    problem: str  # one sentence, what is happening
    likely_cause: str  # why, tied to evidence
    fix: list[str]  # concrete steps, most likely first
    confidence: Literal["high", "medium", "low"]
    evidence_used: list[str]  # which signals support the cause
    rule_hits: list[str] = []
```

**CLI commands (`cli.py`):**

| Command | What it does |
| --- | --- |
| `pfa diagnose <name> -n <namespace>` | Diagnose a pod or workload |
| `pfa diagnose --build-log build.txt` | Diagnose a Docker build failure |
| `pfa diagnose ... --output json` | Machine-readable output for CI or Slack later |
| `pfa diagnose ... --no-llm` | Rules only; no data leaves the machine |
| `pfa diagnose ... --show-evidence` | Print the redacted evidence that was sent |
| `pfa feedback <incident-id> --correct/--wrong` | Mark a diagnosis right or wrong for the eval data |
| `pfa eval` | Run the evaluation suite and print accuracy |

**Configuration (`config.py`):** environment variables or `~/.config/pfa/config.yaml` for provider and model, API key, Prometheus URL, kubeconfig context, and a `send_data` switch that must be on before any hosted LLM is called.

## Deployment: container, Helm chart, RBAC

The tool runs locally against your kubeconfig first, then installs into a cluster with a Helm chart that is read-only by default.

**Local mode:** `uv tool install .` then `pfa diagnose ...` using the current kubeconfig context. This is the v1 path.

**In-cluster mode:**

- Container image: slim Python base, non-root user, read-only root filesystem.
- Helm chart values: provider, model, API key from an existing Secret, Prometheus URL, `sendData` (default false), namespaces to watch.
- Runs as a Deployment (for the watcher and Slack bot later) or a Job for one-off diagnoses.

**RBAC: a ClusterRole with read-only verbs only.**

```yaml
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "events"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets", "statefulsets"]
    verbs: ["get", "list", "watch"]
```

No access to Secrets, no `create`, `update`, `patch` or `delete`. A namespaced Role variant ships for teams that refuse cluster-wide access.

**Install docs** (part of the deliverable): one page covering prerequisites, `helm install`, how to run fully local with Ollama, exactly which data leaves the cluster and when, and how to uninstall.

## Testing and evaluation

Three test layers keep the tool correct, and an evaluation suite measures how often the diagnosis is right.

| Layer | What it covers | How |
| --- | --- | --- |
| Unit tests | Redaction, rules, context builder, schema parsing | pytest with fixture JSON of pod states and fake LLM replies |
| Integration tests | Collectors against a real cluster | kind cluster in CI; apply broken manifests, run collectors, assert evidence |
| Evaluation suite | End-to-end diagnosis quality | `pfa eval` over labelled cases, with the real model |

**Evaluation suite (`eval/`):**

- Each case is a folder: a broken manifest (or build log) plus `expected.yaml` with the failure type and the key words a correct cause must mention.
- Start with 3 cases per failure type (18 total), then add every real failure you meet.
- `pfa eval` deploys each case to kind, waits for the failure, diagnoses it and scores: failure type correct, cause correct, fix usable.
- Results are saved per run with model name and date, so a prompt change shows up as a number going up or down.
- Incidents marked with `pfa feedback --wrong` become new cases.

## Milestones and build order

Five milestones, each ending in something usable, with the first external deployment placed before any new integrations.

&#91;embedded content: milestones · 5 steps, each with a done-when gate\]

Each gate must pass before the next milestone starts. Keep a short build log throughout: decisions, what broke, feedback, and what changed because of it.

## Open decisions and risks

The language choice is the one decision to make before writing code.

- [ ] **Language:** Python (fastest to build, used in this plan) or Go (native to the Kubernetes ecosystem, slower to build).
- [ ] **Default model:** which hosted model to use by default, and which local model works well enough through Ollama.
- [ ] **First external user:** whose cluster gets the first real deployment.

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Secrets leak to an LLM API | Tool unusable in any company | Redaction tests, `sendData` off by default, `--show-evidence`, local model option |
| Confident wrong answers | Developers lose trust fast | Confidence levels, evidence citations, eval suite, "unsure" fallback |
| Context too large or noisy | Slow, costly, worse answers | Structured summaries, trimmed logs, token budget |
| Clusters differ (logging, metrics, CNI) | Collectors break in the field | Optional collectors that degrade gracefully, adapter interface |
| Scope creep | Nothing ships | Six failure types and a CLI first; everything else waits for milestone 5 |

Step-by-step build tasks with Claude Code prompts: Build tasks
