# Broken-pod lab

Fifteen small apps, each broken on purpose in a realistic way, three per pod failure type `pfa`
diagnoses. Everything lives in the `pfa-lab` namespace and is created or deleted with one
command. These cases are also the evaluation suite: `pfa eval` scores diagnoses against them.

```bash
make lab-up       # apply all cases, wait until each shows its failure (~40 s)
make lab-status   # table of cases and whether each is broken yet
make lab-down     # delete the pfa-lab namespace (~40 s, pods get their grace period)
make lab-reset    # down + up: fresh restart counts and events
make lab-snapshot # save each case's `pfa collect` output to cases/<id>/evidence.json
```

Only runs against local clusters (context `kind-*`, `minikube`, `docker-desktop`, `k3d-*`);
set `PFA_LAB_ALLOW_CONTEXT=1` to override. Uses only `busybox:1.36` and `polinux/stress`.

## Cases

| Case | Workload | Failure | What is actually wrong |
| --- | --- | --- | --- |
| `crashloop-missing-env` | deployment/checkout-api | CrashLoopBackOff | `DATABASE_URL` env var is not set; app exits 1 on startup |
| `oomkilled-low-limit` | deployment/report-worker | OOMKilled | Loads ~250MB, memory limit is 128Mi |
| `imagepull-bad-tag` | deployment/storefront | ImagePullBackOff | Tag typo: `1.36.1-uclbic` instead of `1.36.1-uclibc` |
| `pending-cpu-units` | deployment/ml-trainer | Pending | CPU request `"4000"` (4000 cores, stored as `4k`) instead of `"4000m"` |
| `probe-wrong-port` | statefulset/inventory-api | Failing probe | App listens on 80, liveness probe checks 8080 |
| `crashloop-bad-command` | deployment/payment-gateway | CrashLoopBackOff | Command `/app/payment-gateway` is not in the image |
| `crashloop-init-migration` | deployment/orders-api | CrashLoopBackOff (init) | `migrate` init container fails: index already exists |
| `oomkilled-sidecar` | deployment/search-api | OOMKilled | `log-shipper` sidecar buffers ~200MB with a 64Mi limit; main container is fine |
| `oomkilled-growing-cache` | statefulset/session-cache | OOMKilled | Cache warms up to ~300MB, limit is 200Mi; runs for a few seconds first |
| `imagepull-unknown-registry` | deployment/billing-ui | ImagePullBackOff | Registry host `registry.pfa-lab.invalid` does not exist |
| `imagepull-invalid-name` | deployment/notify-worker | InvalidImageName | Two tags: `busybox:1.36:latest` |
| `pending-node-selector` | deployment/video-encoder | Pending | `nodeSelector` matches no node |
| `pending-missing-configmap` | deployment/email-sender | Pending (ContainerCreating) | Mounted ConfigMap `email-templates` does not exist |
| `probe-readiness-404` | deployment/catalog-api | Failing probe | Readiness probe asks for `/ready`; app serves `/healthz` |
| `probe-slow-start` | deployment/ledger-api | Failing probe | App needs 45 s to start; liveness probe kills it after a few seconds |

`checkout-api` also carries a fake secret (`DB_PASSWORD=pfa-fake-secret-value`) and, like many
real apps, prints it in its startup log. The integration tests assert the value never appears
in `pfa` output and that the line comes through as `password=<REDACTED:password>`.

`make lab-up` counts a case as ready only once its rollout is complete, so after a manifest
change it never reports the old pod's failure.

Pulling through Docker Desktop's built-in registry mirror changes some image errors: the
unknown registry answers `500 Internal Server Error` there, not `no such host` as on plain kind.

## Case layout

```
cases/<case-id>/
  manifest.yaml    # the broken app
  expected.yaml    # ground truth
  evidence.json    # saved `pfa collect -o json`, replayed offline by tests/test_lab_cases.py
```

`expected.yaml` fields:

| Field | Used by |
| --- | --- |
| `workload`, `failure_type` | which app to diagnose and what failure it is |
| `root_cause`, `fix` | the right answer, in words, for people reading the case |
| `cause_keywords`, `fix_keywords` | what `pfa eval` looks for in the answer's cause and fix. A nested list accepts any of its alternatives |
| `known_gap` | optional: why the rules are expected to miss this case. Tests fail once it is fixed, until the field is removed |
| `rules_confidence` | optional: the confidence the rules should reach (default `high`) |
| `ready_when` | `lab.py`: when the case counts as broken (`restarts_at_least`, `waiting_reason`, `last_terminated_reason`, `pod_condition_reason`) |
| `evidence_must_contain` | integration tests: strings `pfa collect` output must contain, or the evidence is not enough to diagnose the case |

## Adding a case

1. Create `cases/<case-id>/manifest.yaml` (label pods `app: <workload name>`) and `expected.yaml`.
2. Add the manifest to `kustomization.yaml`.
3. `make lab-up && make lab-snapshot && make test && make test-integration`. The new case is
   picked up automatically, and `make eval` scores it.

Every real failure you meet in the wild is a candidate for a new case.
