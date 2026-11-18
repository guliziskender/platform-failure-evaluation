#!/usr/bin/env bash
# Read-only checks before installing pfa on a cluster: run it with the kube context you will
# install with. It changes nothing (only get/list and `auth can-i`), and every check may fail
# on its own without stopping the rest: a guest often cannot list everything.
#
#   deploy/preflight.sh            # current context
#   deploy/preflight.sh -n pfa     # the namespace you will install into (default: pfa)
set -uo pipefail

NS=pfa
[[ "${1:-}" == "-n" && -n "${2:-}" ]] && NS=$2

ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mcheck\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mno\033[0m    %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

head_ "Cluster"
ctx=$(kubectl config current-context 2>/dev/null) || { bad "no kube context"; exit 1; }
server=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
echo "  context  $ctx"
echo "  server   $server"
version=$(kubectl version -o json 2>/dev/null | sed -n 's/.*"gitVersion": "\(v[^"]*\)".*/\1/p' | tail -1)
if [[ -n "$version" ]]; then echo "  version  $version"; else bad "cannot reach the API server"; fi
# A new autoscaling cluster (GKE Autopilot) can have no nodes yet: an empty list, not an error.
nodes_ok=1
nodes=$(kubectl get nodes -o jsonpath='{range .items[*]}{.spec.providerID} {.status.nodeInfo.architecture}{"\n"}{end}' \
  2>/dev/null) || nodes_ok=0
provider=$(head -1 <<<"$nodes" | cut -d' ' -f1)
case "$provider:$version" in
  aws://*|*-eks-*)   cloud=eks ;;
  gce://*|*-gke.*)   cloud=gke ;;
  azure://*)         cloud=aks ;;
  :*)                cloud=unknown ;;
  *)                 cloud=other ;;
esac
echo "  cloud    $cloud${provider:+ ($provider)}" | cut -c1-100
arches=$(awk 'NF {print $2}' <<<"$nodes" | sort -u | tr '\n' ' ')
if (( ! nodes_ok )); then
  warn "cannot list nodes (fine for pfa, which never reads nodes)"
elif [[ -z "$arches" ]]; then
  warn "no nodes yet: an autoscaling cluster adds them when pods need them (a few minutes)"
elif [[ "$arches" =~ ^((amd64|arm64)\ )+$ ]]; then
  ok "node architectures: $arches(the image has amd64 and arm64)"
else
  bad "node architectures: $arches; the image has only amd64 and arm64"
fi

head_ "Can you install it? (the chart creates these)"
missing=0
for what in "create namespaces" "create clusterroles" "create clusterrolebindings" \
            "create serviceaccounts -n $NS" "create deployments -n $NS" \
            "create configmaps -n $NS" "create networkpolicies -n $NS"; do
  # shellcheck disable=SC2086
  if [[ "$(kubectl auth can-i $what 2>/dev/null)" == yes ]]; then ok "$what"
  else bad "$what"; missing=1; fi
done
if (( missing )); then
  echo "  -> Ask the owner to run the install, or for rbac.scope=namespaces (Roles instead of"
  echo "     a ClusterRole; needs create roles/rolebindings in each namespace)."
fi
if [[ "$(kubectl auth can-i create pods/exec -n "$NS" 2>/dev/null)" == yes ]]; then
  ok "create pods/exec -n $NS (to run pfa inside)"
else
  bad "create pods/exec -n $NS: you cannot kubectl exec into pfa; use the diagnose Job (--set diagnose.name=<pod> --set diagnose.namespace=<ns>)"
fi

head_ "Admission policies"
if out=$(kubectl get ns -L pod-security.kubernetes.io/enforce --no-headers 2>/dev/null); then
  levels=$(awk '{print ($NF ~ /^(privileged|baseline|restricted)$/) ? $NF : "unset"}' <<<"$out" \
    | sort | uniq -c | awk '{printf "%s %s, ", $1, $2}')
  echo "  Pod Security enforce labels across namespaces: ${levels%, }"
  ok "the chart meets \"restricted\" (non-root, read-only root fs, no capabilities, seccomp)"
else
  warn "cannot list namespaces; ask which Pod Security level new namespaces get"
fi
if hooks=$(kubectl get validatingwebhookconfigurations -o name 2>/dev/null); then
  engines=$(grep -oiE 'kyverno|gatekeeper|opa|datree|kubewarden|polaris' <<<"$hooks" | sort -u \
    | tr '\n' ' ')
  if [[ -n "$engines" ]]; then
    warn "policy engine found: ${engines}; ask for its rules (labels, registries)"
  else
    ok "no Kyverno/Gatekeeper webhooks ($(grep -c . <<<"$hooks") admission webhooks in total)"
  fi
else
  warn "cannot list admission webhooks; ask if Kyverno or Gatekeeper is installed"
fi
if quotas=$(kubectl get resourcequota,limitrange -n "$NS" --no-headers 2>/dev/null) \
   && [[ -n "$quotas" ]]; then
  warn "quotas or limit ranges in $NS:"
  while IFS= read -r ln; do echo "        $ln"; done <<<"$quotas"
fi

head_ "Network (only if you enable networkPolicy)"
ds=$(kubectl get daemonsets -n kube-system -o name 2>/dev/null | sed 's|.*/||' | tr '\n' ' ')
if [[ -z "$ds" ]]; then
  warn "cannot list kube-system daemonsets; ask whether NetworkPolicy is enforced"
else
  enforcer=$(grep -oE '\b(calico-node|cilium|anetd|azure-npm|aws-network-policy-agent|kube-router|antrea-agent|weave-net|kindnet|canal)\b' \
    <<<"$ds" | sort -u | tr '\n' ' ')
  [[ "$cloud" == eks ]] && kubectl get ds aws-node -n kube-system \
    -o jsonpath='{.spec.template.spec.containers[*].name}' 2>/dev/null \
    | grep -q network-policy && enforcer+="aws-node (network policy agent) "
  if [[ -n "$enforcer" ]]; then ok "a NetworkPolicy enforcer is running: $enforcer"
  else warn "no known NetworkPolicy enforcer in kube-system ($ds); a policy may do nothing"; fi
fi
api=$(kubectl get endpointslice -n default -l kubernetes.io/service-name=kubernetes \
  -o jsonpath='{.items[*].endpoints[*].addresses[*]}' 2>/dev/null)
if [[ -n "$api" ]]; then
  echo "  API server addresses: $api"
  case "$cloud" in
    eks) echo "  -> EKS replaces these addresses on control-plane updates: allow the VPC's subnet CIDRs,"
         echo "     not /32s." ;;
    aks) echo "  -> AKS may reach the API through a tunnel or a public IP; test the policy before relying"
         echo "     on it." ;;
    gke) echo "  -> GKE: use the control plane's address range (gcloud container clusters describe ...)." ;;
  esac
else
  warn "cannot read the kubernetes EndpointSlice; ask for the API server's addresses"
fi

head_ "Image"
echo "  pfa pulls ghcr.io/guliziskender/pfa. Nodes need egress to ghcr.io (and"
echo "  pkg-containers.githubusercontent.com); private nodes need a NAT or a mirror"
echo "  (--set image.repository=<registry>/pfa)."

head_ "Something to diagnose"
if pods=$(kubectl get pods -A --no-headers 2>/dev/null); then
  failing=$(awk '$4 !~ /^(Running|Completed|Succeeded)$/ || ($4=="Running" && $3 ~ /^0\//)' <<<"$pods")
  if [[ -n "$failing" ]]; then
    echo "  $(wc -l <<<"$failing" | tr -d ' ') pod(s) not running or not ready:"
    head -15 <<<"$failing" | awk '{printf "        %-20s %-45s %s\n", $1, $2, $4}'
  else
    ok "every pod is running (good for them; ask for a staging namespace with past failures)"
  fi
else
  warn "cannot list pods in all namespaces; you will need namespace names from the owner"
fi
echo
