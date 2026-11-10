# Evidence fixtures, one per failure type

Real `pfa collect -o json` output captured from the `kind-pfa-dev` cluster on 2026-10-18,
right after `make lab-reset` (fresh failure states, not days of restarts). Used by
`tests/test_fixtures_by_type.py`. Unlike `lab/cases/*/evidence.json`, these are never
overwritten by `make lab-snapshot`, so unit tests have stable inputs.

| File | Captured from | How it was broken |
| --- | --- | --- |
| `crash_loop_backoff.json` | `pfa-lab/checkout-api` | lab case `crashloop-missing-env` |
| `oom_killed.json` | `pfa-lab/report-worker` | lab case `oomkilled-low-limit` |
| `image_pull_backoff.json` | `pfa-lab/storefront` | lab case `imagepull-bad-tag` |
| `pending.json` | `pfa-lab/ml-trainer` | lab case `pending-cpu-units` |
| `probe_failure.json` | `pfa-lab/inventory-api` | lab case `probe-wrong-port` (StatefulSet) |
| `unknown.json` | `pfa-fixture-quota/billing-api` | temporary namespace with `ResourceQuota pods: "0"`; the Deployment can never create a pod (FailedCreate: exceeded quota). Namespace deleted after capture |
| `none.json` | `kube-system/coredns` | healthy pod, for the "no failure detected" path |

To recapture one: reproduce the setup, then `pfa collect <name> -n <ns> -o json > <file>`.
Check that `pfa-fake-secret-value` does not appear in the result.
