# Platform Failure Assistant (pfa)

CLI that diagnoses Kubernetes and Docker failures in plain language. Full plan: `implementation.md`
(design) and `docs/plan.md` (summary). Build log: `docs/log.md`, so add an entry when a task lands.

## Commands

```bash
uv sync                          # install deps (Python 3.12)
uv run pfa collect <name> -n <ns> [-o json]
uv run pfa diagnose <name> -n <ns> --no-llm [-o json] [--show-evidence]
uv run pfa diagnose <name> -n <ns> --show-prompt   # the exact prompt the AI would get; sends nothing
uv run pfa diagnose <name> -n <ns> --provider anthropic --send-data   # needs ANTHROPIC_API_KEY
uv run pfa diagnose <name> -n <ns> --provider ollama  # local model, no send_data needed
uv run pfa diagnose <name> -n <ns> -o markdown        # for tickets / chat
uv run pfa config [show|init|path]                    # effective settings + their sources
uv run pfa diagnose --from-evidence lab/cases/<id>/evidence.json --no-llm
make lab-up / lab-status / lab-down   # broken-pod lab in ns pfa-lab (local clusters only)
make lab-snapshot                # refresh lab/cases/*/evidence.json after changing collectors
uv run pytest                    # unit tests (no cluster needed)
make test-integration            # brings the lab up, runs pfa collect on every lab case
make test-llm                    # AI diagnosis of every lab case with the configured provider
make eval                        # score the rules on saved evidence; saves to eval/results/
make eval-live                   # lab up, then score on evidence read from the running lab
uv run pfa eval --history        # saved runs over time; --fail-under N exits 1 for CI
uv run pfa eval --provider anthropic --send-data  # score a model the same way
uv run ruff check . && uv run ruff format .
```

## Layout

- `src/pfa/models.py`: `Target`, `Evidence`, `FailureType`. Shared by every layer.
- `src/pfa/config.py`: `Settings.load(flags=...)`: defaults < config file < env < flags, with
  `sources` per setting. `OPTIONS` is the one table of settings (env var, parser, help). Strict:
  unknown keys and ambiguous values raise `ConfigError`. Adding a setting: a `Settings` field
  plus an `OPTIONS` entry (a test checks they match), the README table and `CONFIG_TEMPLATE`.
- `src/pfa/kube.py`: the only place kubeconfig is loaded.
- `src/pfa/engine/target.py`: name -> Target (pod, Deployment, StatefulSet; picks most broken pod/container).
- `src/pfa/engine/orchestrator.py`: `run_diagnosis` (collect core evidence, classify, collect
  type-specific evidence, analyze) and `analyze` (pure: evidence in, `Report` out). No K8s- or
  LLM-specific code here.
- `src/pfa/engine/classifier.py`: evidence -> `FailureType` + problem sentence. Check order matters
  (probe kills look like crashes with exit 137; OOM is checked before both; a failing init
  container keeps the pod Pending, so Pending steps aside for it). "Running and ready"
  is not healthy if the container failed recently (`EvidenceView.failed_recently`, measured
  from evidence collection time, never the wall clock, so replays stay deterministic).
- `src/pfa/ai/redact.py`: secret patterns -> typed placeholders (`<REDACTED:jwt>`).
  `redact_evidence` marks `Evidence.redacted` and counts `redactions`.
- `src/pfa/ai/context.py`: `build_prompt(report)`: fixed section order, ~6k token budget (logs
  trimmed from the head first, then events). Refuses unredacted evidence.
- `src/pfa/ai/prompts/`: `SYSTEM` prompt and per-failure-type `GUIDANCE`.
- `src/pfa/ai/llm.py`: `explain(report, provider)`: ask, validate into `LLMAnswer`
  (`ANSWER_SCHEMA` is hand-written to match), one repair retry, `check_answer` sanity checks,
  fallback to the rules answer with a `Report.notes` entry. Never raises for provider problems.
- `src/pfa/ai/providers/`: `LLMProvider.complete(system, user, schema) -> str`;
  `anthropic.py` (structured outputs), `ollama.py` (httpx). `make_provider` enforces
  `send_data`.
- `src/pfa/ai/rules.py`: `@rule` functions -> `RuleHit`; `answer_from_rules` builds the `--no-llm`
  `Diagnosis` (best hit, or confidence "unsure").
- `src/pfa/evaluation.py`: `pfa eval`: `replay` (`lab/cases/*/evidence.json`) or `live` (read
  the running lab) gives a report per case; `score` compares it with `expected.yaml` (failure
  type, cause, fix). A rules fallback during an AI run counts as an error, never as the model's
  answer. Full runs are saved in `eval/results/` (committed); a run with no answers is not.
  Snapshots hold every collector's output, while a live diagnosis collects by failure type, so
  the two can differ: check both.
- `src/pfa/view.py`: `EvidenceView`, the accessors classifier and rules use. They work only on
  evidence summaries, never live API objects, so saved evidence replays identically.
- `src/pfa/collectors/`: deterministic evidence: `k8s_state`, `k8s_events`, `logs`.
- `lab/`: broken-pod lab, three cases per failure type (`cases/<id>/manifest.yaml` + `expected.yaml`
  ground truth). `lab/lab.py` writes to the cluster, so it is deliberately not part of `pfa`.

## Rules (non-negotiable)

- **Read-only.** Only `get`/`list`/`watch` on pods, pods/log, events, deployments, replicasets,
  statefulsets. Adding any API call means updating the RBAC in the Helm chart and the README.
- **Never read Secret objects or env var values**, only names (`env_names`, `env_from`).
  `tests/test_collectors.py` and the integration test assert the fake secret never appears.
- **All evidence is redacted before anything reads it.** `collect_evidence` redacts each
  collector's result (summary, excerpt and error); `analyze` redacts again for replayed files.
  Code that sends data off the machine (the AI layer) calls `require_redacted` and runs
  `redact_text` on the final prompt. A new secret shape gets a case in
  `tests/fixtures/fake_secrets.py`; build fake tokens from parts so scanners don't flag the repo.
  Redaction must not touch messages rules match on: add those to `BENIGN` there.
- **Nothing leaves the machine unless asked.** No provider = rules only. A provider whose host is
  not localhost needs `send_data` on (`make_provider` refuses otherwise; `data_flow` describes
  it). The CLI announces every send on stderr before it happens. The API key is a
  `repr=False` field, shown only as set/not set; a key in a group/world-readable config file is
  refused.
- **The AI's answer is validated, never shown raw.** It must parse into `LLMAnswer`;
  `check_answer` lowers confidence that the evidence does not earn. Any AI failure falls back to
  the rules answer with a note saying why.
- **Collectors never raise.** They run under `run_safely`; failures become `Evidence.error`.
  Every API call passes `_request_timeout=clients.timeout`.
- Summaries are structured and compact (`drop_none`); raw text only as a trimmed `raw_excerpt`.
- **Rules claim "high" only when the evidence pins down the cause**, not just the symptom. A
  rule's `cause` must cite what it saw (the value, the message), so the answer is checkable.

## Testing conventions

- Unit tests use real API JSON captured from kind in `tests/fixtures/` (strip `managedFields`),
  deserialized with `to_k8s()`, and the `FakeCore`/`FakeApps` clients in `tests/conftest.py`.
- `tests/fixtures/evidence/<failure_type>.json`: real `pfa collect -o json` output, one per
  failure type including `unknown` and `none` (provenance in its README). Stable inputs for
  `tests/test_fixtures_by_type.py`; unlike `lab/cases/*/evidence.json`, never auto-overwritten.
- After a mutation check (temporarily breaking code to see tests fail), delete `__pycache__`:
  a same-size edit within the same second can leave stale bytecode in use.
- New failure shape: add a lab case (see `lab/README.md`); `tests/integration/test_lab.py` picks it
  up automatically and checks `evidence_must_contain`. Capture its pod JSON as a unit fixture too.
- When a collector change is needed to diagnose a lab case, add the signal to that case's
  `evidence_must_contain` so it can't regress.
- `tests/conftest.py::isolated_config` (autouse) clears `PFA_*`/`ANTHROPIC_API_KEY` and points
  `XDG_CONFIG_HOME` at a tmp dir, so tests never read your real config (except `llm` tests).
- AI layer: unit tests use `FakeProvider` and `llm_reply()` from `tests/conftest.py`; adapters
  are tested against mocked HTTP (`httpx.MockTransport`; the Anthropic SDK needs `httpx2`).
  Tests never call a real model; `tests/integration/test_llm_lab.py` (`make test-llm`) does.
- A case the rules miss today keeps its failing answer key and gets a `known_gap` in
  `expected.yaml`: `tests/test_lab_cases.py` then expects the miss, and fails when it is fixed.
- New rule: unit test it in `tests/test_rules.py` with `make_view()` (synthetic summaries). Lab
  cases are checked offline by `tests/test_lab_cases.py` (replays `evidence.json` against
  `expected.yaml`); fix the rule, not the answer key, when one fails.
