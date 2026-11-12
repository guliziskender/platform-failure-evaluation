# Platform Failure Assistant (`pfa`)

[![ci](https://github.com/guliziskender/platform-failure-evaluation/actions/workflows/ci.yml/badge.svg)](https://github.com/guliziskender/platform-failure-evaluation/actions/workflows/ci.yml)

Turns Kubernetes and Docker failures into a plain-language diagnosis with a concrete fix.
Read-only: it never changes anything in the cluster.

## Install (local)

```bash
uv sync                 # dev environment
uv run pfa --help
# or install the `pfa` command globally (editable: code changes apply without reinstalling)
uv tool install --editable .
pfa --help
```

## Run in a cluster (Helm)

```bash
helm install pfa oci://ghcr.io/guliziskender/charts/pfa --version 0.3.0 -n pfa --create-namespace
kubectl -n pfa exec deploy/pfa -- pfa diagnose checkout-api -n shop
```

Read-only by design: get/list/watch on `pods`, `pods/log`, `events`, `persistentvolumeclaims`,
`deployments`, `replicasets` and `statefulsets`, and nothing else (no Secrets, no writes). A per-namespace
variant is one flag, and an optional NetworkPolicy limits pfa to DNS, the API server and the AI
provider. The chart and a multi-arch image are published to ghcr.io by CI for
every release, after the tests pass on a kind cluster. Nothing leaves the cluster unless you set a provider and `ai.sendData=true`.
Full guide, including the data table and uninstall: [deploy/README.md](deploy/README.md).

## Commands

```bash
# Diagnose a failing pod, Deployment or StatefulSet from deterministic rules only
pfa diagnose checkout-api -n pfa-lab --no-llm
pfa diagnose checkout-api -n pfa-lab --no-llm -o json          # for CI / scripts
pfa diagnose checkout-api -n pfa-lab --no-llm -o markdown      # paste into a ticket or chat
pfa diagnose checkout-api -n pfa-lab --no-llm --show-evidence  # plus the evidence used
pfa diagnose --from-evidence saved.json --no-llm                # replay `pfa collect -o json`

# Collect the raw evidence for a pod, Deployment or StatefulSet (no AI involved)
pfa collect memhog -n default
pfa collect memhog -n default --output json
pfa collect api --kind statefulset -n prod --context my-cluster

# Diagnose a failed docker build (no cluster involved). Plain progress shows each step's output.
docker build --progress=plain . 2>&1 | tee build.log
pfa diagnose --build-log build.log --no-llm
docker build --progress=plain . 2>&1 | pfa diagnose --build-log - --no-llm   # or from stdin
```

For a build, `pfa` finds the failing step, its Dockerfile line and the output line that names
the cause (often not the last one), and recognises: a file excluded by `.dockerignore` or outside
the build context, a base image tag that does not exist, a pinned package version that does not
exist, a dependency with no wheel for the image's Python (and a floating base tag such as
`python:3` that moved), a host the build cannot resolve, a missing command or apt package.
BuildKit and the legacy builder are both read.

Example:

```
Failure type  crash_loop_backoff
Problem       Container api is in CrashLoopBackOff: last exit code 1 (Error), 3 restarts.
Likely cause  The app exits on startup because the required environment variable
              DATABASE_URL is not set on the container.
Fix           1. Add DATABASE_URL to the container's env (from a Secret if it holds
              credentials) and roll out again
Confidence    High · rules only (based on crash_missing_env_var)
Evidence      • log: FATAL: required environment variable DATABASE_URL is not set
              • env names on container: DB_USER, DB_PASSWORD, LOG_LEVEL

Rule findings
  high  crash_missing_env_var  The app's logs say environment variable DATABASE_URL is missing.
  low   crash_log_error        The container exits with code 1; its logs report an error.
```

Failure types: `crash_loop_backoff`, `oom_killed`, `image_pull_backoff`, `pending`,
`probe_failure`, `unknown` (something is wrong, but not one of those) and `none` (healthy).

When no rule matches, the rules answer `unsure` instead of guessing.

## AI diagnosis

Without `--no-llm`, `pfa diagnose` asks an AI model, giving it the redacted evidence and the
rule findings as hints. The reply must be valid structured JSON (problem, cause, fix,
confidence, evidence used) or it is not shown. `pfa` lowers the AI's confidence when it cites
no evidence, or when it disagrees with a high-certainty rule. If the AI fails (timeout, rate
limit, invalid reply twice), you get the rules answer and a note saying why.

```bash
# Claude (hosted): evidence leaves your machine, so send_data must be on
export ANTHROPIC_API_KEY=sk-ant-...
pfa diagnose checkout-api -n pfa-lab --provider anthropic --send-data

# Fully local with Ollama: nothing leaves your machine, no send_data needed
ollama pull qwen3:8b
pfa diagnose checkout-api -n pfa-lab --provider ollama

# See exactly what would be sent, without sending it
pfa diagnose checkout-api -n pfa-lab --show-prompt
```

Before anything is sent off your machine, `pfa` says so on stderr:
`Sending redacted evidence (~1,822 tokens) to api.anthropic.com (claude-opus-5-5)`.

## Configuration

Settings come from four layers; the later one wins: built-in defaults, the config file,
environment variables, command-line flags.

```bash
pfa config init    # create ~/.config/pfa/config.yaml (commented, mode 600)
pfa config         # every setting, its value, where it came from, and what data leaves
pfa config path    # where pfa looks for the file ($PFA_CONFIG, else $XDG_CONFIG_HOME/pfa/)
```

```yaml
# ~/.config/pfa/config.yaml
provider: anthropic
send_data: true
```

| Setting (file) | Env variable | Flag | Default |
| --- | --- | --- | --- |
| `provider` | `PFA_PROVIDER` | `--provider` | none (rules only); `anthropic`, `ollama` |
| `model` | `PFA_MODEL` | `--model` | `claude-opus-5-5` / `qwen3:8b` |
| `send_data` | `PFA_SEND_DATA` | `--send-data` / `--no-send-data` | off |
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | | prefer the env var; a key in the file needs `chmod 600` |
| `ollama_url` | `PFA_OLLAMA_URL` | | `http://localhost:11434` |
| `llm_timeout` | `PFA_LLM_TIMEOUT` | | 30 s per AI call, 120 s for Ollama (rate limits and 5xx retried twice; timeouts are not retried for Ollama) |
| `prompt_budget_tokens` | `PFA_PROMPT_BUDGET_TOKENS` | | 6000 |
| `kube_context` | `PFA_KUBE_CONTEXT` | `--context` | kubeconfig's current context |
| `api_timeout` | `PFA_API_TIMEOUT` | | 10 s per Kubernetes call |
| `log_tail_lines`, `log_max_bytes`, `event_window_minutes` | `PFA_LOG_TAIL_LINES`, ... | | 200, 4096, 60 |

Mistakes are errors, not silently ignored: an unknown key (`send-data: true` gets "did you
mean 'send_data'?"), `PFA_SEND_DATA=maybe`, a negative timeout, or an API key in a config file
that other users can read.

`pfa` uses your current kubeconfig context and only issues `get`/`list`
calls against pods, pods/log, events, persistentvolumeclaims, deployments, replicasets and
statefulsets.

## What data `pfa` reads, and what it hides

- It never reads Secret objects or env var **values**, only env var names.
- Everything it does collect (pod status, events, logs, error messages) is redacted before it
  is analysed, printed or saved. Passwords, tokens, API keys, JWTs, private keys and
  credentials in URLs are replaced with typed placeholders such as `<REDACTED:password>`, so
  you can still see that something was there. `--show-evidence` shows exactly what `pfa` kept,
  with a count of what it redacted:

  ```
  │ db config: user=checkout password=<REDACTED:password>                  │
  ╰───────────────────────────────────────────────────── redacted: 1 password ─╯
  ```
- With `--no-llm`, with no provider, or with a local Ollama, nothing leaves your machine. A
  hosted provider (or a remote Ollama) is used only when `send_data` is on, and only ever
  receives redacted evidence. `--show-prompt` prints exactly what it would receive, and
  `pfa config` states in one line what leaves the machine with your current settings.

## Broken-pod lab

`make lab-up` deploys 21 deliberately broken apps to the `pfa-lab` namespace of a local cluster;
`make lab-down` removes them. Five broken Dockerfiles sit next to them, with their real `docker
build` output (`uv run python lab/lab.py build-logs` re-captures it). See
[lab/README.md](lab/README.md).

## Evaluation

`pfa eval` scores diagnoses against 26 labelled lab cases: 18 standard ones (three per failure
type, Docker builds included) and 8 hard ones built to mislead (misleading logs, a red-herring
event, two failures at once, causes outside the pod or the Dockerfile). Is the failure type right, does the cause state the key facts, does the fix? It also counts answers that
were confident and wrong, since those do the most damage.

```bash
make eval                                          # score the rules on the saved evidence
make eval-live                                     # bring the lab up, score on what it shows now
uv run pfa eval --provider anthropic --send-data   # score a model (sends the redacted lab evidence)
uv run pfa eval --provider ollama --model qwen3:8b # score a local model
uv run pfa eval --history                          # every saved run, oldest first
uv run pfa eval --no-llm --fail-under 80           # exit 1 under 80% correct
uv run pfa eval --no-llm --fail-on-new-misses      # CI: exit 1 on any miss not recorded as a known_gap
```

The default replays each case's saved evidence, so it needs no cluster and every model sees the
same input. `--live` reads the running lab (`pfa` only reads; `make lab-up` deploys it), which also
tests target selection and the collectors. Use both: live found a problem replay could not.

Each full run is saved in `eval/results/` with its model, evidence mode and date. The next run by
the same answerer on the same evidence names the cases that got fixed or broke. A case where the
AI or the cluster gave no answer counts as wrong. A run with no answers at all exits 2 and is not
saved, because it would say the provider is down, not how good the answers are.

Current results (2026-11-11, saved evidence):

| Answered by | Standard (18) | Hard (8) | Of which Docker builds | Confident and wrong | Seconds per case |
| --- | --- | --- | --- | --- | --- |
| rules (`--no-llm`) | 18/18 | 8/8 | 5/5 | 0 | instant |
| claude-opus-5-5 | 17/18 | 8/8 | 5/5 | 1 | 4 to 19 |
| qwen3:8b via Ollama (local, Apple M5, 24 GB) | 18/18 | 8/8 | 5/5 | 0 | 19 to 44 |

Opus's one miss is on a pod case it passed in three earlier runs (`crashloop-init-migration`):
its answer was right, but its cause did not say the failing container is the `migrate` init
container (its fix did). The answer key asks the cause to name it, and stays as it is. Model
answers vary between runs; one run is one sample. The rules' build rules were written while
reading the five build logs, so their 5/5 is not an unseen-case score; the models' is.

The hard cases first scored rules 1/6, Opus 5/6 and qwen3:8b 4/6, with 3, 1 and 3 answers
confident and wrong. Every miss pointed at something in pfa, not only in the answerers:

- The rules quoted the last error line (a shutdown side effect), diagnosed one container of
  two, and trusted the first probe failure over the one that repeats.
- No rule recognised a hostname that does not resolve, so a missing Service got a generic fix.
- The volume-claim rule claimed high confidence from the pod's side alone, while the reason
  was on the claim, which pfa did not read. Every answerer missed that case; Opus guessed the
  claim did not exist. pfa now reads the claims a pod mounts (one more read-only permission).
- qwen3:8b missed the second container even with its logs in the prompt: the prompt's rule
  hints held only the first container's finding, at high certainty. They now cover every
  failing container.
- qwen3:8b wrote a path as `\u002Fapp\u002Fpayment-gateway`; pfa now decodes such escapes.

The rules were then fixed on these cases, so their 6/6 says less than the first run did. The
models were not tuned; they improved because the evidence and hints did. `docs/log.md`
(2026-11-11) has the detail.

Earlier lower runs in the history are real records, not noise: an Opus 14/15 came from a
too-strict answer key, another from a bug in pfa's event collection, and qwen3:8b's 3/15 and 4/15
were timeouts, not wrong answers (see `docs/log.md`, 2026-11-10).

The rules were tuned with the standard cases in view, so a perfect score there says less than a
score on cases nobody has tuned against. The hard cases were written before any answerer saw them,
and their answer keys were not changed after. The model was not tuned on them. Every real failure is a
candidate for a new case. Misses show the answer's own words (saved in the run's JSON), so a
too-strict answer key is easy to spot.

The cause and fix checks look for keywords, so they show an answer states the key facts, not that
its fix would work.

## Tests

```bash
make test               # unit tests, no cluster needed
make test-integration   # pfa collect and diagnose --no-llm against every lab case
make test-llm           # AI diagnosis of every lab case (needs a provider; sends fake lab data)
make test-helm          # build the image, install the chart locally, diagnose from inside it
```
