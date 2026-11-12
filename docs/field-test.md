# Field test: pfa on a cluster I don't control (milestone 4)

Goal: install pfa where I am a guest, diagnose real failures, and learn what breaks, what is
missing and what the owner thinks. Everything here is read-only, and uninstalling leaves nothing
behind.

## 1. Ask the cluster owner

Send them `deploy/README.md` ("What pfa can access" and "What data leaves the cluster") and ask:

- [ ] May I install pfa? The chart creates: one namespace, a service account, a read-only
      ClusterRole (or a Role per namespace), a ConfigMap and one small pod (requests 10m CPU and
      96Mi memory, memory limit 256Mi).
- [ ] Cluster-wide read, or only some namespaces? (`rbac.scope=namespaces`)
- [ ] Rules only, or may redacted evidence go to an AI model? Which one? Default: rules only,
      nothing leaves the cluster.
- [ ] Can the cluster pull from ghcr.io? If not: which registry should I push the image to?
- [ ] Policies I should know about: Pod Security admission level, Kyverno/Gatekeeper rules,
      network policies, required labels.
- [ ] A time window, and who to tell when I'm done.

## 2. Install (15 minutes)

```bash
kubectl config current-context        # the right cluster?
kubectl auth can-i create clusterrole # or ask the owner to run the install
helm install pfa oci://ghcr.io/guliziskender/charts/pfa --version 0.3.1 -n pfa --create-namespace
#   only some namespaces: --set rbac.scope=namespaces --set 'rbac.namespaces={team-a,team-b}'
#   own registry:         --set image.repository=<registry>/pfa
kubectl -n pfa rollout status deploy/pfa
kubectl -n pfa exec deploy/pfa -- pfa --version
```

Check it is as read-only as promised, with the API server's own answer:

```bash
SA=system:serviceaccount:pfa:pfa
for v in "get pods" "get pods/log" "list events" "get secrets" "delete pods" "create pods/exec"; do
  echo "$v: $(kubectl auth can-i $v -n default --as $SA)"
done
```

## 3. Use it

Find what is failing now, and diagnose each one:

```bash
kubectl get pods -A | grep -vE 'Running|Completed'
kubectl -n pfa exec deploy/pfa -- pfa diagnose <name> -n <namespace>
kubectl -n pfa exec deploy/pfa -- pfa diagnose <name> -n <namespace> -o markdown > <name>.md
```

Also try a recent failed CI build log: `pfa diagnose --build-log build.log` (on my machine).

## 4. Record, for each diagnosis

| Field | |
| --- | --- |
| Workload, failure type pfa gave | |
| Was the cause right? (yes / partly / no, and what it really was) | |
| Was the fix usable as written? | |
| Confidence pfa gave; was it earned? | |
| Seconds | |
| Anything redacted that should not have been, or shown that should not? | |

Every miss becomes a lab case (`lab/README.md`, "Adding a case"), written before any fix.

## 5. Ask the owner afterwards

- Would you leave it installed? What would stop you?
- Which answer was most / least useful?
- What failure did it not cover that you see every week?
- Was the install and the access it asked for acceptable as is?

## 6. Uninstall

```bash
helm uninstall pfa -n pfa && kubectl delete namespace pfa
```

Then write the results into `docs/log.md` (milestone 4), with the owner's words where possible.
