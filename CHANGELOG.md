# Changelog

Install a release: `helm install pfa oci://ghcr.io/guliziskender/charts/pfa --version <version>`.
Image: `ghcr.io/guliziskender/pfa:<version>`. Detail for each change: `docs/log.md`.

## 0.3.0 (2026-11-12)

- `pfa diagnose --build-log FILE` (or `-` for stdin) diagnoses a failed `docker build`: the
  failing step, its Dockerfile line, and the output line that names the cause. BuildKit and the
  legacy builder. Rules for files excluded by `.dockerignore`, missing base image tags, pinned
  package versions that do not exist, dependencies with no wheel for the image's Python
  (naming a floating base tag such as `python:3`), hosts the build cannot resolve, missing
  commands and apt packages. `pfa collect --build-log` shows the parsed evidence.
- Five Docker build lab cases with real build logs (`lab.py build-logs`); the eval has 26 cases.
  Rules, claude-opus-5-5 and qwen3:8b: 26/26, 25/26 and 26/26.
- Helm: optional NetworkPolicy (`networkPolicy.enabled`): no inbound traffic; outbound only to
  DNS, the API server and the configured AI provider. `make test-helm` checks a live cluster
  enforces it.

**Upgrading from 0.2.0:** nothing to do; the NetworkPolicy is off unless enabled.

## 0.2.0 (2026-11-11)

Diagnoses failures that mislead: six new hard lab cases, and the fixes they pointed at.

- Every failing container in a pod is diagnosed, not just the most broken one. pfa reads each
  one's logs and names each cause.
- Crashes: the most severe log line is the cause, not the last one; an env var whose value the
  app rejects is named; a hostname that does not resolve is reported as a missing Service; a
  config file that does not exist is reported with where to mount it.
- Probes: the current failure counts, not the first. A 5xx readiness probe whose app cannot
  reach its database says the probe is right and names the dependency.
- Pending on a volume claim: pfa reads the claim and its events, so it names the cause (e.g. a
  StorageClass that does not exist). From the pod's side alone it now says medium, not high.
- AI answers: the prompt covers every failing container; leftover `\uXXXX` escapes are decoded.
- Eval: 21 cases (15 standard, 6 hard, scored apart). Rules, claude-opus-5-5 and qwen3:8b each
  21/21, none confident and wrong. CI gates on misses not recorded as a `known_gap`.

**Upgrading from 0.1.0:** the chart grants one more read-only permission, `get`/`list`/`watch`
on `persistentvolumeclaims`; `helm upgrade` applies it. Without it (a custom role), pfa still
works and only loses the claim's details.

## 0.1.0 (2026-11-11)

First release: `pfa diagnose` for CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending and
failing probes, from rules or an AI model (Anthropic, or Ollama locally), with redacted
evidence; `pfa eval` on 15 lab cases; Helm chart with read-only RBAC; CI that tests on kind and
publishes the image and chart.
