#!/usr/bin/env bash
# Build the pfa image and make it available to a local cluster without a registry.
#   deploy/load-image.sh [context]   (default: kubectl's current context)
set -euo pipefail
IMAGE=pfa:dev
CONTEXT=${1:-$(kubectl config current-context)}

docker build -q -t "$IMAGE" "$(dirname "$0")/.." >/dev/null
case "$CONTEXT" in
  kind-*)
    kind load docker-image "$IMAGE" --name "${CONTEXT#kind-}" ;;
  docker-desktop)
    # Docker Desktop's cluster runs its own containerd; import the image into its node.
    docker save "$IMAGE" | docker exec -i desktop-control-plane ctr -n k8s.io images import - >/dev/null ;;
  minikube)
    minikube image load "$IMAGE" ;;
  *)
    echo "don't know how to load images into context '$CONTEXT'; push $IMAGE to a registry" >&2
    exit 2 ;;
esac
echo "$IMAGE is available in $CONTEXT"
