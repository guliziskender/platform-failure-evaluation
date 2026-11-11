# Platform Failure Assistant (`pfa`)

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
helm install pfa deploy/helm/pfa -n pfa --create-namespace --set image.repository=<your registry>/pfa
kubectl -n pfa exec deploy/pfa -- pfa diagnose checkout-api -n shop
```

Read-only by design: get/list/watch on `pods`, `pods/log`, `events`, `deployments`,
`replicasets` and `statefulsets`, and nothing else (no Secrets, no writes). A per-namespace
variant is one flag. Nothing leaves the cluster unless you set a provider and `ai.sendData=true`.
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
```

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
calls against pods, pods/log, events, deployments, replicasets and statefulsets.

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

`make lab-up` deploys five deliberately broken apps (one per failure type) to the `pfa-lab`
namespace of a local cluster; `make lab-down` removes them. See [lab/README.md](lab/README.md).

## Evaluation

`pfa eval` scores diagnoses against the 15 labelled lab cases (three per pod failure type): is the
failure type right, does the cause state the key facts, does the fix? It also counts answers that
were confident and wrong, since those do the most damage.

```bash
make eval                                          # score the rules on the saved evidence
make eval-live                                     # bring the lab up, score on what it shows now
uv run pfa eval --provider anthropic --send-data   # score a model (sends the redacted lab evidence)
uv run pfa eval --provider ollama --model qwen3:8b # score a local model
uv run pfa eval --history                          # every saved run, oldest first
uv run pfa eval --no-llm --fail-under 80           # exit 1 under 80% correct, for CI
```

The default replays each case's saved evidence, so it needs no cluster and every model sees the
same input. `--live` reads the running lab (`pfa` only reads; `make lab-up` deploys it), which also
tests target selection and the collectors. Use both: live found a problem replay could not.

Each full run is saved in `eval/results/` with its model, evidence mode and date. The next run by
the same answerer on the same evidence names the cases that got fixed or broke. A case where the
AI or the cluster gave no answer counts as wrong. A run with no answers at all exits 2 and is not
saved, because it would say the provider is down, not how good the answers are.

Current results (2026-11-10):

| Answered by | Evidence | Correct | Confident and wrong | Seconds per case |
| --- | --- | --- | --- | --- |
| rules (`--no-llm`) | replay and live | 15/15 | 0 | instant |
| claude-opus-5-5 | replay and live | 15/15 | 0 | 6 to 14 |
| qwen3:8b via Ollama (local, Apple M5, 24 GB) | replay and live | 15/15 | 0 | 24 to 94 |

Earlier lower runs in the history are real records, not noise: an Opus 14/15 came from a
too-strict answer key, another from a bug in pfa's event collection, and qwen3:8b's 3/15 and 4/15
were timeouts, not wrong answers (see `docs/log.md`, 2026-11-10).

The local model matches the hosted one on these cases, but is 5 to 10 times slower, and nothing
leaves the machine. These cases are clear-cut; harder or noisier failures may separate them.

The rules were tuned with these cases in view, so a perfect score here says less than a score on
cases nobody has tuned against. The model was not tuned on them. Every real failure is a
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
