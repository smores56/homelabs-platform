#!/usr/bin/env bash
# Flux bootstrap for the homelab cluster.
#
# Needs a reachable cluster (KUBECONFIG) and a GitHub token with admin on the
# repository. CI never runs this: it creates cluster state and pushes a commit.
#
# Tenancy lockdown is carried by the controller patches in
# clusters/homelab/kustomization.yaml, so the bootstrap command stays the plain
# documented one. The two --insecure-kubeconfig-* flags appear nowhere in this
# repository; tests/policies/check.sh fails if either is added.
set -euo pipefail

owner="smores56"
repository="homelabs-platform"
branch="main"
cluster_path="clusters/homelab"
root_sa="kustomize-controller"
sync_file="clusters/homelab/flux-system/gotk-sync.yaml"

usage() {
  echo "usage: $0 [--print-sync]" >&2
  echo "  --print-sync  print the canonical gotk-sync.yaml and exit" >&2
}

# Single source of truth for the root sync objects. `flux bootstrap` writes its
# own version of this file (no service account, so the controller-wide
# --default-service-account=default would apply cluster-scoped objects as an
# identity without RBAC), so bootstrap re-renders it from here afterwards.
render_sync() {
  cat <<YAML
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: flux-system
  namespace: flux-system
spec:
  interval: 1m0s
  ref:
    branch: ${branch}
  secretRef:
    name: flux-system
  url: ssh://git@github.com/${owner}/${repository}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: flux-system
  namespace: flux-system
spec:
  interval: 10m0s
  path: ./${cluster_path}
  prune: true
  timeout: 10m
  serviceAccountName: ${root_sa}
  sourceRef:
    kind: GitRepository
    name: flux-system
YAML
}

case "${1:-}" in
  --print-sync)
    render_sync
    exit 0
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  "") ;;
  *)
    usage
    exit 2
    ;;
esac

for tool in flux git kubectl; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "missing required tool: $tool" >&2
    exit 1
  }
done

cd "$(git rev-parse --show-toplevel)"

echo "context: $(kubectl config current-context)"
read -r -p "bootstrap ${owner}/${repository} into this context? [y/N] " reply
[[ $reply == y || $reply == Y ]] || {
  echo "aborted"
  exit 1
}

flux check --pre

flux bootstrap github \
  --owner="${owner}" \
  --repository="${repository}" \
  --branch="${branch}" \
  --path="${cluster_path}" \
  --personal \
  --components-extra=source-watcher \
  --interval=10m

git pull --ff-only

render_sync >"${sync_file}"
git add "${sync_file}"
if ! git diff --cached --quiet -- "${sync_file}"; then
  git commit -m "chore(flux): pin the root Kustomization to ${root_sa}"
  git push
fi

kubectl -n flux-system get kustomizations.kustomize.toolkit.fluxcd.io
echo "next: docs/runbook.md steps 1-3 (sops-age, tenant git credential, ESO resume)"
