# Evidence from real clusters outside the lab

`pfa collect -o json` output from clusters other than kind, each one a case pfa got wrong at
first. Project IDs and other account names are replaced (`example-project`). Never
auto-overwritten.

| File | Cluster | What it shows |
| --- | --- | --- |
| `gke_autopilot_no_nodes.json` | GKE Autopilot 1.36, two minutes after creation, zero nodes | kube-dns Pending: "no nodes available to schedule pods"; its ReplicaSet's earlier FailedCreate (a quota still being set up) was followed by SuccessfulCreate of this pod. pfa first blamed the quota. |
| `gke_autopilot_scale_up.json` | the same cluster, pfa's own pod as the first user workload | Pending with a TriggeredScaleUp event: Autopilot added the first node only once a user workload arrived. |
| `gke_autopilot_scheduled_after_scale_up.json` | the same cluster, once the node was up | kube-dns Running and ready, with the hour-old FailedScheduling and FailedCreate events still there. pfa (master at the time) called it "pending: no nodes". |
| `gke_scale_up_quota_exceeded.json` | the same cluster, the lab running | Lab case oomkilled-sidecar never started: FailedScaleUp "GCE quota exceeded" (the project's SSD_TOTAL_GB, 489 of 500 GB), then NotTriggerScaleUp "in backoff". pfa first answered "no node has enough free cpu, memory". |
