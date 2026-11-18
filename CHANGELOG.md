# Changelog

Install a release: `helm install pfa oci://ghcr.io/guliziskender/charts/pfa --version <version>`.
Image: `ghcr.io/guliziskender/pfa:<version>`. Detail for each change: `docs/log.md`.

## 0.3.2 (2026-11-18)

Fixes from the first run on a cloud cluster (GKE Autopilot 1.36):

- A Pending pod on an autoscaling cluster: pfa now reads the cluster autoscaler's events. A
  failed scale-up is the cause ("GCE quota exceeded" names the quota to check, per cloud),
  even while a new attempt runs; a scale-up with no failure says wait. It used to answer "no
  node has enough free cpu, memory", true of the current nodes but not the cause.
- A cluster with no nodes ("no nodes available to schedule pods") is diagnosed as that (high),
  not blamed on the pod.
- A pod that is scheduled now is no longer called Pending because of older FailedScheduling
  events; a FailedCreate followed by a successful create is no longer blamed.
- A workload that does not exist: the error says to look at the `kubectl apply` / `helm
  install` output, since one refused by an admission policy leaves nothing in the cluster.
- `deploy/preflight.sh`: read-only checks before installing on someone else's cluster.
- Docs: Autopilot notes in the install guide; the can-i check in `docs/field-test.md` now works
  in zsh.

No chart changes; upgrade with `--reset-then-reuse-values` as before.

## 0.3.1 (2026-11-12)

- Fix: upgrading an existing install to 0.3.0 with `helm upgrade --reuse-values` (as the guide
  said) failed with "nil pointer evaluating ... networkPolicy", even with the policy off:
  `--reuse-values` keeps the old release's values, which have no `networkPolicy`, and does not
  apply the new chart's defaults. The chart now fills them in. The guide now upgrades with
  `--reset-then-reuse-values` (Helm 3.14+). Fresh installs of 0.3.0 were not affected.

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
