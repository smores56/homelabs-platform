#!/usr/bin/env bash
# Renders every kustomization under clusters/. Optional argument: directory to
# write rendered manifests into, one file per kustomization, for kubeconform.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

out_dir="${1:-}"
if [[ -n $out_dir ]]; then
  mkdir -p "$out_dir"
  find "$out_dir" -maxdepth 1 -name '*.yaml' -delete
fi

mapfile -t dirs < <(find clusters -name kustomization.yaml -print0 |
  xargs -0 -n1 dirname | sort)
if ((${#dirs[@]} == 0)); then
  echo "no kustomization.yaml under clusters/" >&2
  exit 1
fi

for dir in "${dirs[@]}"; do
  echo "build $dir"
  if [[ -n $out_dir ]]; then
    kustomize build "$dir" >"$out_dir/${dir//\//_}.yaml"
  else
    kustomize build "$dir" >/dev/null
  fi
done
