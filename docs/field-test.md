# Field test: pfa on a cluster I don't control (milestone 4)

Goal: install pfa where I am a guest, diagnose real failures, and learn what breaks, what is
missing and what the owner thinks. Everything here is read-only, and uninstalling leaves nothing
behind. Results go in `docs/field-test-results.md`.

## 1. Ask the cluster owner

Send them this (edit the brackets), with `deploy/README.md` attached or linked:

> Hi [name], I'm building pfa, an open-source CLI that explains why a Kubernetes pod is failing
> (CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending, probe failures) in plain language:
> https://github.com/guliziskender/platform-failure-evaluation
>
> I'd like to try it on [cluster] for [an hour] on [date], to see whether its answers hold up on
> a real cluster. What it needs:
>
> - **Read-only access:** get/list/watch on pods, pod logs, events, PVCs, deployments,
>   replicasets and statefulsets. It cannot read Secrets, ConfigMaps or env var values, and it
>   cannot change anything. Cluster-wide, or only the namespaces you choose.
> - **What it installs:** one namespace, a service account, the read-only role, a ConfigMap and
>   one small pod (10m CPU, 96Mi memory, limit 256Mi). It meets Pod Security "restricted".
>   `helm uninstall` removes all of it.
> - **Data:** by default nothing leaves the cluster; the diagnosis comes from built-in rules.
>   With your OK only, redacted evidence (secrets masked) can go to an AI model: Anthropic's
>   API, or a local model.
>
> Questions: May I install it, or would you rather run the install yourself? Cluster-wide or
> which namespaces? Rules only, or is the AI model OK? Can the nodes pull from ghcr.io? Any
> policies I should know about (Kyverno/Gatekeeper, required labels, network policies)?
> Who should I tell when I'm done? Afterwards I'd like 10 minutes of your feedback.

Write down the answers in the results file before starting.

## 2. Pre-flight (read-only, 2 minutes)

```bash
kubectl config use-context <their-cluster>
deploy/preflight.sh            # or: deploy/preflight.sh -n <namespace you will install into>
```

It changes nothing: it shows the cluster and cloud, whether you can create what the chart
creates, Pod Security levels and policy engines, whether NetworkPolicy is enforced and the API
server's addresses, and which pods are failing now. Paste its output into the results file.

On a cloud cluster:

| | EKS | GKE | AKS |
| --- | --- | --- | --- |
| Get credentials | `aws eks update-kubeconfig --name <c> --region <r>` | `gcloud container clusters get-credentials <c> --region <r>` | `az aks get-credentials -g <rg> -n <c>` |
| NetworkPolicy enforced? | only with the VPC CNI's network policy agent (or Calico/Cilium) | Dataplane V2 or network policy enabled | Azure NPM, Calico or Cilium chosen at creation |
| API server addresses for `networkPolicy.apiServer.cidrs` | change on control-plane updates: use the VPC subnet CIDRs | the control plane's range (`gcloud container clusters describe`) | can be a tunnel or public IP: test before relying on it |
| Image pull from ghcr.io | private subnets need a NAT gateway | private nodes need Cloud NAT | usually fine |

The NetworkPolicy is optional. Skip it for the first install, and add it only if the owner wants
egress locked down.

## 3. Install (10 minutes)

```bash
helm install pfa oci://ghcr.io/guliziskender/charts/pfa --version 0.3.1 -n pfa --create-namespace --wait
#   only some namespaces: --set rbac.scope=namespaces --set 'rbac.namespaces={team-a,team-b}'
#   own registry:         --set image.repository=<registry>/pfa
kubectl -n pfa get pods
kubectl -n pfa exec deploy/pfa -- pfa --version     # pfa 0.3.1
```

If the pod is not Running: `kubectl -n pfa describe pod -l app.kubernetes.io/name=pfa` and
`kubectl -n pfa get events`. A failed install is a result too: record it.

Check it is as read-only as promised, with the API server's own answer:

```bash
# one check per line; works in bash and zsh (zsh does not split "$v" into words)
while read -r verb res; do
  echo "$verb $res: $(kubectl auth can-i "$verb" "$res" -n default --as system:serviceaccount:pfa:pfa)"
done <<'EOF'
get pods
get pods/log
list events
get secrets
get configmaps
delete pods
create pods/exec
EOF
# yes, yes, yes, no, no, no, no
```

## 4. Use it

```bash
kubectl get pods -A | grep -vE 'Running|Completed'          # what is failing now
kubectl -n pfa exec deploy/pfa -- pfa diagnose <name> -n <namespace>
kubectl -n pfa exec deploy/pfa -- pfa diagnose <name> -n <namespace> --show-evidence
kubectl -n pfa exec deploy/pfa -- pfa diagnose <name> -n <namespace> -o markdown > <name>.md
```

Nothing failing? Ask the owner for a namespace that had trouble recently: pfa reads restart
history and events from the last hour. A healthy pod should come back as healthy ("none");
that is worth one check too. Also try a recent failed CI build log on your own machine:
`pfa diagnose --build-log build.log`.

Before you write anything down, check every claim against the cluster yourself (`kubectl
describe`, `kubectl logs --previous`). The owner is the final judge of the cause.

## 5. Record each diagnosis

In `docs/field-test-results.md`: one row per diagnosis. Copy no secrets and no customer data:
if an answer quotes something sensitive, that is itself a finding (redaction missed it), so
describe it without copying it. Each miss becomes a lab case (`lab/README.md`, "Adding a
case"), written before any fix.

## 6. Ask the owner afterwards

- Would you leave it installed? What would stop you?
- Which answer was most / least useful?
- What failure did it not cover that you see every week?
- Was the install and the access it asked for acceptable as is?

## 7. Uninstall

```bash
helm uninstall pfa -n pfa && kubectl delete namespace pfa
kubectl get clusterrole,clusterrolebinding | grep pfa       # nothing left
```

Tell the owner it is gone. Then add a milestone 4 entry to `docs/log.md`, with the owner's words
where possible.
