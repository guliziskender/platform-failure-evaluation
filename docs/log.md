# Build log

## 2026-10-09: Task 1: project skeleton and `pfa collect`

**Built:** uv/Python 3.12 project, `pfa collect <name>` with three collectors (`k8s_state`,
`k8s_events`, `logs`), target resolution for pods, Deployments and StatefulSets, text and JSON
output. 26 unit tests on fixtures captured from kind, 5 integration tests against kind.

**Decisions**
- Language: Python (closes the open decision in the plan).
- `collect` before `diagnose`: inspecting the raw evidence first makes the AI layer debuggable,
  and the JSON output becomes the replayable input for the eval suite.
- Name lookup order: pod, then Deployment, then StatefulSet; `--kind` skips auto-detection.
- Deployment pods are matched by ReplicaSet ownership (uid), not only labels, so another
  workload with the same labels can't be picked by mistake.
- A Deployment with zero pods still resolves; the newest ReplicaSet is kept as an owner because
  that is where `FailedCreate` events (quota, admission) land.
- Env var values and Secrets are dropped in the collector itself, not left for redaction later.

**What broke / surprised**
- Repeated events didn't merge: kubelet puts timings in messages (`pulled in 813ms`, `in 1.137s`).
  The dedupe key now replaces numbers and duration units; Warnings sort first so the 30-event cap
  drops Normal noise.
- `previous=True` logs for a fast crash loop returned HTTP 200 with the text
  `unable to retrieve container logs for containerd://…` (container already garbage-collected).
  Now reported as unavailable instead of passed on as log content.
- A Pending pod returns empty logs (200), not an error.

**Next:** the plan's open items: classifier, rules pre-check, redaction module, RBAC additions
(nodes and PVCs for Pending; ControllerRevisions for the StatefulSet deploy diff).

## 2026-10-09: Task 2: broken-pod lab

**Built:** `lab/` with five broken apps in namespace `pfa-lab`, one per pod failure type:
checkout-api (CrashLoopBackOff, missing `DATABASE_URL`), report-worker (OOMKilled, 128Mi limit vs
~250MB), storefront (ImagePullBackOff, tag typo), ml-trainer (Pending, CPU `"4000"` instead of
`"4000m"`), inventory-api (StatefulSet, liveness probe on 8080 while the app serves 80).
`make lab-up` / `lab-down` / `lab-status` / `lab-reset`. Up takes ~37 s, down ~40 s.

**Decisions**
- Realistic causes (missing env var, typos, unit mistake, wrong port), not bare `exit 1`s. The
  diagnosis has to reason about the cause, not just read the status.
- Each case has an `expected.yaml` with the ground truth now, so Task 7 only adds scoring.
- `evidence_must_contain` per case: the integration test checks collection separately from
  diagnosis. If a later diagnosis is wrong, we know whether the evidence was there.
- The lab script lives outside the `pfa` package and refuses non-local contexts, because `pfa`
  itself must stay read-only.
- Only cached images (busybox, polinux/stress), so the lab needs no new image pulls and comes up fast.
- Replaced the Task 1 `tests/integration/manifests/broken.yaml` / `pfa-test` setup: one set of
  broken apps, not two.

**What broke / surprised**
- The probe case could not be diagnosed from Task 1 evidence: `k8s_state` did not collect
  container ports, so "probe on 8080, app on 80" was invisible. Added `ports` (`http=80/TCP`).
- Kubernetes normalises the CPU request `"4000"` to `"4k"` in the stored spec. The diagnosis must
  read `4k` as 4000 cores; a good eval case.
- `lab-down` takes ~40 s because `sh` as PID 1 ignores SIGTERM, so each pod waits out its 30 s
  grace period. Left as is to keep manifests realistic.

## 2026-10-09: Task 3: failure classifier and rules (`--no-llm`)

**Built:** `pfa diagnose <name> --no-llm` prints Problem / Likely cause / Fix / Confidence, plus
`-o json`, `--show-evidence` and `--from-evidence` (replay a saved `pfa collect -o json`).
- Classifier (`engine/classifier.py`): evidence -> one of the five pod failure types, `none`
  (healthy) or `unknown`, plus a one-sentence problem.
- 23 rules with 27 distinct findings (`ai/rules.py`) covering OOM, image pulls (not found, auth, rate limit, network, invalid
  name), scheduling (resources, CPU unit typo, node selector, taints, PVCs, mounts), probes
  (wrong port, not listening, HTTP status, timeout), crashes (missing env var, missing
  ConfigMap/Secret, exit 127/126/0/139, log error fallback) and FailedCreate (quota, policy).
- `make lab-snapshot` saves each lab case's evidence. `tests/test_lab_cases.py` replays it
  offline against `expected.yaml`.
- Result: all 5 lab cases diagnosed correctly with high confidence, both on a long-running lab
  and right after `lab-reset`. 100 unit and 12 integration tests.

**Decisions**
- Classifier and rules work on evidence summaries only (via `EvidenceView`), never live API
  objects. Live and replayed evidence give identical answers, which is the basis for Task 7.
- Two-phase collection: state and events, then classify, then only the collectors that fit the
  type (no logs for Pending or ImagePull).
- No rule match means confidence "unsure" with pointers, never a guess. Added `unsure` to the
  confidence values and `none` (healthy) to `FailureType`, closing two gaps from the plan review.
- Hits are ranked by matching the classified type first, then certainty. So a low-certainty
  "error in logs" never beats a high-certainty specific cause.
- Without `--no-llm`, `diagnose` prints a note that no AI provider exists yet and answers from
  rules, instead of pretending.

**What broke / surprised**
- A liveness-probe kill looks exactly like a crash: CrashLoopBackOff plus exit 137 "Error". Only
  the `Killing ... failed liveness probe` event tells them apart, so probes are checked before
  crashes. Regression test: `test_probe_kill_is_not_mistaken_for_a_crash`.
- The scheduler message says `Insufficient cpu.` with a full stop. The first regex captured
  `cpu.`, so the CPU unit-typo rule never fired. Resource names can contain dots
  (`nvidia.com/gpu`), so the regex now requires a non-dot last character.
- The image-pull answer was right, but its cause said "does not exist" without quoting the
  registry's "not found". The ground-truth keyword check caught it. The rule now cites the
  registry's answer (the answer key stays as is).
- The env-var regexes required the words "environment variable". The bare form
  `DATABASE_URL is not set` was missed, so I added an UPPER_SNAKE pattern (the underscore
  avoids matching words like `ERROR`).
- Rich parses `[...]` in cluster messages as markup, so all cluster text is rendered via `Text()`.

**Next:** redaction module, then the LLM provider. Rule hits become hints in the prompt.

## 2026-10-18: Task 3 follow-up: spec check, per-type fixtures, crash-loop "up window" bug

Re-checked Task 3 against its written spec (I had built it before getting plan approval) and
closed the gaps.

**Changed**
- `pfa diagnose` now prints the failure type first and a **Rule findings** section listing
  every rule hit with its certainty and finding. Before, only the best hit's cause was shown
  and the other rules were listed by name.
- `tests/fixtures/evidence/`: real captured evidence for each failure type, including `unknown`
  (Deployment blocked by `ResourceQuota pods: "0"` in a temporary namespace, deleted after
  capture) and `none` (coredns). `tests/test_fixtures_by_type.py` checks the type, best rule,
  certainty and cause for each. These are kept separate from the lab snapshots so
  `make lab-snapshot` cannot silently change unit test inputs.
- Verified against every workload in `pfa-lab` (4 Deployments and 1 StatefulSet) after a
  `lab-reset`: all classified correctly, each with a high-certainty finding.

**What broke / surprised**
- A mutation check (swapping the probe and crash checks) did not fail the new probe fixture
  test. Cause: the fixture was captured **between restarts**, when the container was running
  and ready (liveness-only containers count as ready while up). Following that up exposed a real
  bug: a crash-looping app caught in its "up" window was reported as **"No failure detected"**,
  even with BackOff warnings. Fixed with `EvidenceView.failed_recently()`: an abnormal exit
  within 1 h of collection, or BackOff restart events, means still failing. The same check
  stops an OOM from days ago being reported as a current failure on a recovered pod. Recency
  is measured from evidence collection time, so replays give the same answer days later.
  Five regression tests; a second mutation check confirmed they fail without the fix.
- The first mutation check also produced phantom failures afterwards: the mutated and restored
  files had the same size and were written within the same second, so Python reused stale
  bytecode. Noted in CLAUDE.md.

## 2026-10-26: Task 4: secret redaction

Goal: no secret can leave the machine, in place before any AI call exists.

**Built**
- `src/pfa/ai/redact.py`: patterns for private key blocks (including a block whose BEGIN line
  was cut off by log trimming), credentials in URLs, JWTs, AWS/GitHub/Slack/Google/Stripe/
  Anthropic/OpenAI keys, Authorization headers, `key=value` / `key: value` / JSON pairs whose
  key names a secret, and `--password value` flags, including flag and value split across
  container args. Matches become typed placeholders (`<REDACTED:password>`); the user and host
  of a URL stay, because they help the diagnosis.
- Two choke points: `collect_evidence` redacts every collector result (summary, excerpt,
  error), and `analyze` redacts again, so `--from-evidence` files that are hand-edited or
  older are covered. `Evidence` now has `redacted` and `redactions` (counts by kind), and
  `require_redacted` is the guard the AI layer will call.
- `pfa collect` and `--show-evidence` show a "redacted: 1 password" note per evidence source.
- The `checkout-api` lab app now prints its DB password in its startup log, so the
  integration test that asserts the fake secret never appears tests redaction end to end, not
  just the collector skipping env values. Its `evidence_must_contain` includes
  `password=<REDACTED:password>`, so the line can't silently go missing either.

**Decisions**
- Redact early (right after collection), not just before the LLM call. Rules, terminal output,
  JSON output and saved snapshots all see the same redacted evidence, so there is no second,
  unredacted copy that a later change could leak by accident.
- When in doubt, redact. The exception: Kubernetes messages the rules match on must survive
  untouched. `tests/fixtures/fake_secrets.py` has 33 leaks that must vanish and 20 real
  messages that must come through unchanged, and every captured evidence file (lab snapshots
  and per-type fixtures) is checked to be unchanged by redaction.
- Fake tokens in the test fixture are assembled from parts at import time, so GitHub push
  protection and other secret scanners won't block a push of this repo.

**What broke / surprised**
- My own first patterns had false positives that the benign list caught: `Bearer realm=...`
  (a registry auth challenge) and `may require authorization: server message:` (the
  registry's pull-denied message, which the image-pull rules read). A bare `Bearer x` now
  needs a digit in `x`. An `Authorization:` header with no scheme needs a token-like value.
- kubelet writes `volume "db-password" : secret "db" not found`. A naive `key: value` pattern
  would eat `secret`, breaking the missing-Secret rule, so a space before `:` doesn't count as
  a key/value separator.
- `https://<REDACTED:github_token>@github.com` was redacted again on a second pass (it looks
  like `user:pass@`). The idempotency test caught it.
- Kubernetes' own `$(DB_PASSWORD)` env-expansion syntax in args was treated as a secret value;
  references (`$(X)`, `${X}`, `{{ x }}`, `<your-key>`) are now left alone.
- Lab bug found on the way: after a manifest change, `make lab-up` could say "ready" based on
  the **old** pod, before the rollout replaced it. Readiness now also requires the rollout to
  be complete (`observedGeneration`, `updatedReplicas`, no extra pods).
- Mutation check: disabling redaction in `analyze` fails 3 tests; disabling it in
  `collect_evidence` fails 1.

**Next:** LLM provider adapter. It must call `require_redacted` on its evidence and
`redact_text` on the final prompt, and stay off unless `send_data` is enabled.

## 2026-10-26: Task 5: first LLM diagnosis

Goal: evidence in, structured diagnosis out.

**Built**
- `ai/context.py`: the prompt. Sections in a fixed order (target, classification, type
  guidance, state, events, logs, collector errors, rule hints), about 6k tokens max. Logs are
  trimmed from the head first (the tail holds the crash), then events (Warnings stay). Lab
  prompts come out at 1.3k–1.8k tokens.
- `ai/prompts/`: system prompt (use only the evidence, quote values, earn "high", never guess
  redacted values) and short guidance per failure type.
- `ai/providers/`: one `complete(system, user, schema) -> str` interface. Anthropic uses
  structured outputs (`output_config.format` with a JSON schema), with the SDK's 2 retries and
  backoff on 429/5xx, and a 30 s timeout. Ollama (`/api/chat` with `format`) has its own
  retries. Refusals, cut-off replies and empty replies become `ProviderError`s.
- `ai/llm.py`: validate the reply into `LLMAnswer`, with one repair call on invalid JSON or
  schema violations. `check_answer` then lowers confidence the evidence doesn't earn: no
  evidence cited means low; disagreeing with a high-certainty rule caps it at medium (and
  notes the disagreement). Any failure keeps the rules answer and adds a note.
- CLI: `pfa diagnose` uses the AI unless `--no-llm` is passed. `--show-prompt` prints the exact
  prompt and sends nothing. Notes and "AI (model)" are shown in the output, and rule findings
  are always listed next to the AI answer.
- `make test-llm`: AI diagnosis of every lab case against ground truth. It's separate from
  `make test-integration`, so that never sends data anywhere.

**Decisions**
- `send_data` is enforced in `make_provider`, not in the CLI, so the watcher and Slack bot get
  the same guard later. Local Ollama doesn't need it; remote Ollama does.
- The API key is not a `Settings` field: the SDK reads it from the environment, so it can't end
  up in a printed or logged object.
- Default model: `claude-opus-5-5` (most capable; prompts are small). Local default `qwen3:8b`.
  Both can be overridden with `PFA_MODEL`; Task 7 measures which is good enough.
- Env vars only, no config file yet (the plan mentions `~/.config/pfa/config.yaml`). It's not
  needed until the Helm chart.
- Hand-written `ANSWER_SCHEMA` instead of Pydantic's generated schema: structured-output APIs
  accept a subset of JSON Schema, and this keeps what is sent under review. A test checks it
  matches the model.
- OpenAI adapter deferred: the interface is in place, so it's one file when needed.

**What broke / surprised**
- The prompt builder's final redaction pass ran over the **JSON-encoded** prompt. In there,
  `$DB_PASSWORD"` becomes `$DB_PASSWORD\"`, so the reference stopped matching the "this is a
  reference, not a value" check and was redacted. Worse in principle: a JSON-escaped
  `password=\"abc\"` would be only partly caught. Each piece is now redacted in its natural
  form (structures before encoding, text as text). Regression test:
  `test_references_stay_readable_inside_json`.
- The Anthropic SDK (2.x) ships its own `httpx2` and rejects plain `httpx` clients, so the
  adapter tests mock transport with `httpx2.MockTransport`.

**Not verified yet:** no API key or Ollama on this machine, so no real model has answered.
Everything is tested against mocked HTTP and fake providers. Run `make test-llm` with a
provider configured.

**Next:** Task 7 evaluation suite (score AI and rules answers per case, per model).

## 2026-10-26: Task 6: output, config and the data switch

Goal: make the tool pleasant and safe to use.

**Built**
- Config file (`~/.config/pfa/config.yaml`, or `$PFA_CONFIG` / `$XDG_CONFIG_HOME`), layered as
  defaults < file < env < flags. Every setting records where its value came from.
- `pfa config [show|init|path]`. `show` lists each setting with its source and ends with one
  sentence on what leaves the machine with these settings. `init` writes a commented template
  with mode 600 and never overwrites.
- Data switch: `send_data` from the file, `PFA_SEND_DATA`, or `--send-data/--no-send-data` for a
  single run. Before anything leaves the machine, stderr says how much and where to
  (`Sending redacted evidence (~1,822 tokens) to api.anthropic.com`). A local Ollama says
  "nothing leaves this machine".
- `diagnose --provider/--model` for one-off overrides; `-o markdown` for tickets and chat; a
  `schema_version` in the JSON report; `pfa --version`; a spinner while collecting and while
  waiting for the model.
- Errors: "not found" now names the kube context (the memhog confusion today was a context
  switch made by Docker Desktop). Errors are soft-wrapped, so paths and commands in them stay
  copyable.

**Decisions**
- Strict parsing. A misspelled key or `PFA_SEND_DATA=maybe` is an error, never a silent
  default: a safety switch that is quietly in the wrong position is worse than a refusal.
  Unknown keys get a "did you mean" hint.
- The API key may live in the config file (the plan asks for it), but only if the file is not
  readable by group or others, like ssh. The env var is still the recommended place. The key
  is never printed: `repr=False`, shown as "set", and hidden in error messages.
- `pyyaml` moved from a dev dependency to a runtime one; the `anthropic` minimum was raised to
  2.3, since `output_config` and `httpx2` are what the adapter uses.
- An autouse test fixture isolates config (env vars and the XDG dir) so a developer's own
  `PFA_PROVIDER` can't change unit test results. `llm`-marked tests are exempt, because they
  exist to use the real settings.

**What broke / surprised**
- Rich hard-wraps error messages at terminal width, which split a file path in the middle of
  the "chmod 600 <path>" advice. Errors now print with `soft_wrap` and escaped markup.
- `cli.make_provider` was patched by tests; once the CLI went through `data_flow`, patching
  there would have silently stopped working. The tests now patch `pfa.ai.providers`.

**Next:** Task 7 evaluation suite.

## 2026-11-02: Task 7: evaluation cases

Goal: measure diagnosis accuracy instead of guessing it.

**Built**
- Ten new lab cases, for 15 in total: three each for CrashLoopBackOff, OOMKilled,
  ImagePullBackOff, Pending and failing probes. They add an init container, a sidecar, a
  StatefulSet, a slow start, and a readiness-only failure.
- `pfa eval`: replays each case's saved evidence, gets an answer from the rules or a model, and
  scores failure type, cause and fix against `expected.yaml`. It also counts answers that are
  confident and wrong. `make eval`, `make eval-live`.
- Full runs are saved to `eval/results/<date>-<model>.json` and each run shows the previous score
  by the same answerer.

**Baseline:** rules 13/15, confident and wrong 0. No model scored yet: no API key or Ollama on
this machine.

**Decisions**
- Cases stay in `lab/cases`, not a new `eval/cases`, and `pfa eval` replays saved evidence
  instead of deploying as the plan said. `pfa` itself never writes to a cluster, so deploying
  stays in `lab.py` (`make eval-live` chains them). Replays also cost nothing to repeat, and
  every model sees identical evidence.
- If the AI fails on a case, that case is an error. `explain` falls back to the rules' answer,
  and scoring that would credit the rules' score to the model.
- A provider that is configured but blocked stops the run, for the same reason.
- The two misses stay in as `known_gap`, unfixed, so the fix moves the number.
- Keyword matching is a coarse check: it shows the answer states the key facts, not that a fix
  would work. Good enough to catch regressions, not proof of quality.
- No Docker build cases yet: `pfa diagnose --build-log` does not exist. The plan's 18 cases are
  15 until it does.

**What broke / surprised**
- New rule gap: Docker Desktop pulls through a built-in `registry-mirror`, so an unknown registry
  gives `500 Internal Server Error`, not `no such host`, and no rule matches. The rules answer
  unsure, which is at least honest.
- Confirmed by the suite: a crash-looping init container is classified as pending.
- A first version of the scorer found keyword "80" inside "8080", which would have passed a wrong
  port. Matches can no longer run into more digits.

**Next:** fix the two gaps and the redaction leaks from the code review, then score a real model.

## 2026-11-02: Task 8: pfa eval runner

Goal: turn the scorer from Task 7 into something that tracks accuracy over time and can gate CI.

**Built**
- `pfa eval --live`: reads evidence from the running lab, read-only. `make eval-live` brings the
  lab up first and no longer overwrites the saved snapshots.
- Comparison with the previous run by the same answerer on the same evidence, naming the cases
  that got fixed or broke. Also a per-failure-type breakdown and seconds per case.
- `pfa eval --history` lists saved runs. `--fail-under N` exits 1 under N% correct.
- One case failing to collect no longer stops the run; it counts as wrong.

**Baseline:** rules 13/15 on replay and on live, 0 confident and wrong. Still no model scored.

**Decisions**
- A run with no answers at all exits 2 and is not saved. An API outage would otherwise be stored
  as 0/15 and become the baseline for the next comparison.
- Live and replay runs are never compared with each other: they see different evidence.

**What broke / surprised**
- Replay flattered the init-container case. Snapshots hold every collector's output, but a live
  diagnosis collects logs by failure type. Misclassified as pending, it never reads the logs, then
  the rules say the app "fails before it logs anything" while the log names the exact error. Same
  score of 13/15 both ways, since it was already a miss, but only live showed it. A model scored
  on replay would also see logs that a real run would not give it.
- Saved runs were named by the second, so a replay right after a live run overwrote it. Found
  by reading the output of the first real run: both said the same "Saved to". Names now have
  microseconds and the evidence mode.

**Next:** fix the two gaps and the redaction leaks, then score a real model.

## 2026-11-10: Redaction fixes from the code review

Goal: close the redaction leaks the code review found before any real evidence goes to a hosted
model.

**Fixed** (each is now a case in `tests/fixtures/fake_secrets.py`, and failed before the fix)
- Leaked completely: a quoted value containing the other quote (`password="it's-x"`), JSON
  escaped inside a log line's string (`msg="{\"password\":\"x\"}"`, what structured
  loggers write), and a YAML block value (`client_secret: |` and indented lines).
- Leaked in part: unquoted values with spaces (`password: correct horse battery`) and values
  containing `;` or `&` (`password=ab;cd`).
- Over-redacted: a key at the end of a line took the next line's first word. `password: not set`
  and `API_TOKEN: missing` were mangled, and those are what the missing-env-var rule reads.
- The URL password pattern was quadratic on long hyphenated text (about 11 s per 100KB, and
  container args have no size cap). The scheme length is now bounded. A test times six hostile
  inputs at 100KB; all now take well under a second.

**Decisions**
- After `key: ` an unquoted value runs to the end of the line. After `key=` it ends at
  whitespace, as in env dumps and logfmt, so `password=x user=y` keeps the user. A `,`, `&` or
  `;` ends the value only where the next `key=` starts, so connection strings, query strings
  and comma lists keep their other pairs.
- Over-redaction is the accepted cost: `Set-Cookie: session_id=x; Path=/` now loses the whole
  cookie, attributes included.

**Checked:** the eval score is unchanged (rules 13/15), so the stricter patterns cost no
diagnosis. Mutation check: each of five fixes, undone, makes a test fail.

**Not covered yet:** a YAML value on the line after its key with no `|` (`password:` then an
indented value), and secrets split across log lines in other ways.

## 2026-11-10: Fix the two eval misses

Goal: fix the two cases the rules missed. The eval measures the result: 13/15 before.

**Fixed**
- Init containers. A crash-looping or OOMKilled init container keeps the pod Pending, and the
  Pending check ran first, so it was reported as "containers have not started". Pending now
  steps aside for a failing init container, so the crash and OOM checks handle it. Messages say
  "Init container migrate ..." and that the other containers never start.
- Registry mirror errors. The registry rule only knew network errors. It now also recognises a
  5xx from a registry or proxy. When the failing request went to a mirror (`?ns=<registry>` in
  its URL), the cause names both the mirror and the registry the image comes from.

**Result:** rules 15/15 on replay and on live, 0 confident and wrong. `pfa eval` printed "Fixed
since then: crashloop-init-migration, imagepull-unknown-registry" against the saved baseline.

**Decisions**
- The init case stays at low confidence: the rules point at the error line in its logs, but not
  at why the migration fails. The mirror case is medium: a mirror's 500 does not say whether the
  registry name or the network is wrong. Both are in `rules_confidence`.
- Tests of misses, `--fail-under` and history no longer depend on what the rules get wrong today.
  They use a copy of the cases with one answer key broken on purpose.

**Caveat:** the rules were changed with these two cases in view, so 15/15 is partly a score on
the training set. New cases from real failures are what keep the number honest.

## 2026-11-10: Remaining code-review findings

Goal: fix the smaller findings from the code review.

**Fixed** (each has a test)
- Config:
  - An empty `PFA_CONFIG=` counts as unset, instead of "points to ..., which does not exist".
  - A key with no value in the config file (`model:`) means not set, like an empty env var.
  - `ollama_url` must be an http(s) URL. `localhost:11434` without a scheme was treated as a
    remote host (send_data needed), then failed at request time with a confusing error.
- Pod choice during a rollout: terminating pods rank last. After a fix rolls out, the old
  crashing pod lingers while it shuts down, and pfa diagnosed it. An old-ReplicaSet pod that is
  not terminating is left as is: it is still failing now.
- Wording:
  - A finished Job pod (phase Succeeded) is "completed successfully", not "all containers are
    ready".
  - A container that exited once without a restart (a failed Job) "exited and was not
    restarted", not "keeps exiting ... 0 restarts".
- `lab.py` uses pfa's kube context for every kubectl call and for `snapshot`. Before, `up`
  used kubectl's current context and `snapshot` used `PFA_KUBE_CONTEXT`, so they could read
  different clusters. The local-only guard now checks the context actually used.

**Not changed:** a failed Job is still typed `crash_loop_backoff`. There is no job failure type
yet, and the problem text now says what happened.

**Checked:** rules eval still 15/15; integration tests pass against the live lab.

## 2026-11-10: First real model scored

Goal: score a real model, so the AI path is measured and not just tested against fakes.

**Result:** claude-opus-5-5 scored 14/15 on both replay and live evidence, then 15/15 on replay
after an answer key fix (below). Every failure type and every fix was right. It answered the
init-container case with high confidence, where the rules say low, and it used the exact
numbers in the evidence ("250M is about 238Mi, nearly double the limit"). Six to eleven seconds
per case. The redacted lab evidence (fake data only) was sent to api.anthropic.com.

**What broke / surprised**
- The one miss was the scorer's fault. The model wrote "resources.limits.memory is 128Mi", and
  the answer key wanted the words "memory limit", so it scored a right answer as confident and
  wrong. The other two OOM cases already accepted "limit"; this one now does too. The 14/15 runs
  stay in `eval/results/`, as the record of what was scored at the time.
- Finding that out took another API call, because runs did not save the answer itself. Each
  case result now keeps the cause and fix text, and the misses list shows the answer.

**Decisions**
- Keyword scoring is the weak point, and a stronger model makes it weaker: it phrases things in
  its own words. Answer keys should list the ways a correct answer can say a fact. A judge model
  could score meaning instead of words, but it would cost a second call per case and needs its
  own checks. Not now.
- The default hosted model stays claude-opus-5-5 (one of the plan's open decisions). No local
  model scored yet.

## 2026-11-10: Live re-run found an event-window bug

Goal: re-run the live Opus eval, which still showed the 14/15 from the too-strict answer key.

**Result:** the old miss was gone, but a new one appeared: `probe-slow-start` answered as
crash_loop_backoff instead of probe_failure. The model's cause and fix were right, and it said
why it hedged: "No 'Unhealthy' probe events were included in the evidence."

**Cause:** a bug in pfa, not the model. After hours of crash-looping, the pod's Unhealthy event
was last updated at 18:11 while its BackOff event kept updating (19:38). The kubelet stops
updating some events during a long failure, likely its per-object event rate limit. pfa kept
only the last 60 minutes of events, so the probe evidence fell out, and the classifier and rules
answered a plain crash with "no rule matched". The integration tests caught it too, once the lab
had run long enough. It only showed on a live run against a lab that had been up for a week:
snapshots and a freshly deployed lab both had recent events.

**Fixed:** the target pod's own events are kept whatever their age. The window still applies to
ReplicaSet and Deployment events, where an old event may be about an earlier problem. An old
probe event cannot make a different crash look like a probe kill: the classifier also requires
the current termination to be a kubelet kill (exit 137 or 143).

**After:** claude-opus-5-5 15/15 on live and on replay; rules 15/15 on live. Without the fix,
three integration tests fail on the long-running lab; with it, all pass.

**Lesson:** a long-running lab is a test environment of its own. Events age, counts grow and
the kubelet behaves differently than in the first minutes after `make lab-up`.

