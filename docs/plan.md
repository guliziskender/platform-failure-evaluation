# Platform Failure Assistant (pfa)

## What this project is
An AI-based tool that diagnoses Kubernetes and Docker failures and explains them in plain language with a concrete fix. Goal: let developers unblock themselves and reduce repetitive support load on the platform team.

Example: `pfa diagnose payment-service` →
- Problem: container OOMKilled 3x in 10 min
- Likely cause: new deploy loads a larger cache; peak ~600Mi vs 512Mi limit
- Fix: raise memory limit to 768Mi or lower CACHE_MAX_SIZE
- Confidence: high (exit code 137 + memory metrics + timing matches deploy)

## Scope (v1)
- In scope: Kubernetes and Docker failure analysis
- Out of scope for now: application security
- The tool diagnoses and suggests only. It never changes anything in the cluster.
- Start narrow: CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending pods, failing probes, Docker build failures.

## Architecture
1. **Interfaces**: CLI first (`pfa diagnose <name>`). Later: watcher (listens to K8s events), Slack bot. All call the same engine.
2. **Diagnosis engine**: orchestrator. Identifies the failure, picks collectors, calls the AI layer, validates and formats output. No K8s- or LLM-specific code here.
3. **Evidence collectors** (deterministic, no AI), one shared interface/adapter pattern:
   - Kubernetes API: pod status, exit codes, events, resource limits, deployment spec
   - Logs and events: last N lines of current + previous container
   - Metrics and deploy history: Prometheus memory/CPU trends, diff vs previous ReplicaSet
4. **AI layer**:
   - Redaction + context builder: strip secrets/tokens/env values before anything leaves the cluster; build a compact structured prompt
   - Rules pre-check: answer obvious cases without the LLM (e.g. exit 137 + OOMKilled); pass result to the model as a hint
   - LLM provider adapter: one interface, multiple backends (Claude API, OpenAI API, local via Ollama). Model returns structured JSON: problem, cause, fix, confidence, evidence_used. Validate before showing.
5. **Incident store**: every diagnosis + whether it was correct. SQLite first, Postgres later. Doubles as the evaluation dataset.

## Engineering principles
- Read-only by default. Helm chart with a ServiceAccount limited to get/list/watch on pods, events, deployments, replicasets, plus pods/log.
- Designed to be installed in someone else's cluster: clear setup docs, no outbound data unless configured.
- Handle timeouts, LLM rate limits, and an "unsure" fallback.
- Evaluation suite: broken manifests with known root causes; track diagnosis accuracy over time.
- Keep a short build log (decisions, what broke, feedback, what changed).

## Build order
1. CLI that diagnoses a single pod failure in a local kind/minikube cluster
2. Evaluation set + accuracy measurement
3. Helm chart + read-only RBAC
4. First external deployment (a real cluster I don't control) to get feedback
5. More integrations (Prometheus/Loki, GitHub Actions webhook, Slack), then watcher/Slack bot

## Open decisions
- Language: Python (kubernetes client, Typer, Pydantic, pytest; fastest) vs Go (more native to K8s; slower to build). Not decided yet.

## Context
I'm building this solo with Claude Code. It also serves as a portfolio project for Forward Deployed Engineer roles, so integrations, safe deployment into unfamiliar environments, and measurable results matter as much as features.
