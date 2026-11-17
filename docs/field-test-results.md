# Field test results (milestone 4)

Filled in during the field test (`docs/field-test.md`). No secrets, customer data or real
hostnames here: name workloads by role ("the payments API") if the owner prefers.

## The cluster

| | |
| --- | --- |
| Date | |
| Cloud and version (EKS/GKE/AKS, Kubernetes 1.xx) | |
| Size (nodes, namespaces, pods, roughly) | |
| Who installed (me / the owner) | |
| RBAC scope (cluster / which namespaces) | |
| Provider (rules only / Anthropic / local model) | |
| Pod Security level, policy engines, NetworkPolicy | |

## Owner's answers before the install

-

## Pre-flight output

```
(paste deploy/preflight.sh output; remove hostnames if asked)
```

## Install

| Step | Result | Minutes | Problem, if any |
| --- | --- | --- | --- |
| helm install | | | |
| pod Running | | | |
| `pfa --version` | | | |
| can-i checks (yes yes yes no no no no) | | | |

## Diagnoses

| # | Workload (role) | Real cause (checked how) | pfa's type | pfa's cause right? (yes / partly / no) | Fix usable? | Confidence; earned? | Seconds | Redaction OK? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | | | | | | | | |
| 2 | | | | | | | | |
| 3 | | | | | | | | |

Notes per diagnosis (what pfa said, what it missed, what evidence it lacked):

1.

## Misses -> lab cases

| Diagnosis # | What was missing | Lab case id | Status |
| --- | --- | --- | --- |
| | | | |

## Owner's feedback

- Would you leave it installed? What would stop you?
- Most / least useful answer?
- A weekly failure it did not cover?
- Install and access acceptable?

## Uninstall

- [ ] `helm uninstall` and namespace deleted
- [ ] no pfa ClusterRole / ClusterRoleBinding left
- [ ] owner told
