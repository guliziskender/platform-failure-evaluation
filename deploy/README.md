# Installing pfa in a cluster

pfa runs in your cluster with read-only access: it can read pods, their logs, events and
workloads, and nothing else. By default no data leaves the cluster.

## What pfa can access

The chart creates a ClusterRole (or a Role per namespace, see below) with exactly this:

| API group | Resources | Verbs |
| --- | --- | --- |
| core | `pods`, `pods/log`, `events` | get, list, watch |
| apps | `deployments`, `replicasets`, `statefulsets` | get, list, watch |

No Secrets, no ConfigMaps, no exec, no writes of any kind. pfa reads container env var
*names* from pod specs, never their values. `tests/test_helm.py` checks this list against every
Kubernetes API call in pfa's source, and `make test-helm` asks a live API server
(`kubectl auth can-i`) what the service account can and cannot do.

The pod runs as a non-root user (uid 10001), with a read-only root filesystem, no privilege
escalation, all Linux capabilities dropped, and the runtime's default seccomp profile.

## Prerequisites

- Kubernetes 1.25 or later, and Helm 3.8 or later.
- The pfa image in a registry your cluster can pull from. No public image is published yet:

  ```bash
  docker build -t registry.example.com/platform/pfa:0.1.0 .
  docker push registry.example.com/platform/pfa:0.1.0
  ```

  For a local cluster (kind, Docker Desktop, minikube), `make image` builds `pfa:dev` and loads
  it into the cluster directly; then install with `--set image.tag=dev --set image.pullPolicy=Never`.

## Install (rules only: nothing leaves the cluster)

```bash
helm install pfa deploy/helm/pfa -n pfa --create-namespace \
  --set image.repository=registry.example.com/platform/pfa
```

Diagnose a workload from the small toolbox pod the chart runs:

```bash
kubectl -n pfa exec deploy/pfa -- pfa diagnose checkout-api -n shop
kubectl -n pfa exec deploy/pfa -- pfa diagnose checkout-api -n shop -o markdown
```

Or run one diagnosis as a Job and read its output:

```bash
helm upgrade pfa deploy/helm/pfa -n pfa --reuse-values \
  --set diagnose.name=checkout-api --set diagnose.namespace=shop
kubectl -n pfa logs -l app.kubernetes.io/instance=pfa --tail=-1
```

## Only some namespaces

For teams that do not grant cluster-wide read access: a Role and RoleBinding in each listed
namespace, and no ClusterRole.

```bash
helm install pfa deploy/helm/pfa -n pfa --create-namespace \
  --set rbac.scope=namespaces --set 'rbac.namespaces={shop,payments}'
```

pfa then says so plainly when asked about any other namespace: `no permission to read namespace
'default' (Kubernetes API 403 Forbidden) ...`.

## With an AI model

### Anthropic (hosted: redacted evidence leaves the cluster)

Create the Secret yourself. The chart only references it, so the key is never in Helm values
or release history, and pfa's own service account still cannot read Secrets: the kubelet injects
the key into the pod's environment.

```bash
kubectl -n pfa create secret generic pfa-anthropic --from-literal=ANTHROPIC_API_KEY=sk-ant-...
helm upgrade pfa deploy/helm/pfa -n pfa --reuse-values \
  --set ai.provider=anthropic --set ai.existingSecret=pfa-anthropic --set ai.sendData=true
```

Without `ai.sendData=true`, pfa refuses to send anything and answers from its rules.

### Ollama in your cluster (the model also runs in your cluster)

```bash
helm upgrade pfa deploy/helm/pfa -n pfa --reuse-values \
  --set ai.provider=ollama --set ai.ollamaUrl=http://ollama.ollama.svc:11434 \
  --set ai.model=qwen3:8b --set ai.sendData=true
```

`sendData` is still required: the evidence leaves pfa's pod for another service, and pfa cannot
verify where a URL leads, so it only treats `localhost` as local. An 8B model on CPU is slow:
raise `ai.timeoutSeconds` if diagnoses fall back to the rules with "did not answer within".

## What data leaves the cluster, and when

| Settings | What leaves | Where to |
| --- | --- | --- |
| `ai.provider=none` (default) | nothing | |
| `ai.provider=anthropic`, `ai.sendData=false` | nothing (pfa refuses, answers from rules) | |
| `ai.provider=anthropic`, `ai.sendData=true` | redacted evidence for the workload you diagnose | api.anthropic.com |
| `ai.provider=ollama`, `ai.sendData=true` | redacted evidence | the Ollama URL you set |

Redacted evidence is the pod's state, its events and the tail of its logs, with passwords,
tokens, keys and similar values replaced by `<REDACTED:kind>`. See exactly what would be sent,
without sending it:

```bash
kubectl -n pfa exec deploy/pfa -- pfa diagnose checkout-api -n shop --show-prompt
```

## Uninstall

```bash
helm uninstall pfa -n pfa      # removes the pod, ConfigMap, service account and RBAC
kubectl delete namespace pfa   # and the API key Secret, if you created one there
```

pfa never changed anything in your cluster, so there is nothing else to undo.
