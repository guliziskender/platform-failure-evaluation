# Evidence from real clusters outside the lab

`pfa collect -o json` output from clusters other than kind, each one a case pfa got wrong at
first. Project IDs and other account names are replaced (`example-project`). Never
auto-overwritten.

| File | Cluster | What it shows |
| --- | --- | --- |
| `gke_autopilot_no_nodes.json` | GKE Autopilot 1.36, two minutes after creation, zero nodes | kube-dns Pending: "no nodes available to schedule pods"; its ReplicaSet's earlier FailedCreate (a quota still being set up) was followed by SuccessfulCreate of this pod. pfa first blamed the quota. |
