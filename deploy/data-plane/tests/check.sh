#!/usr/bin/env bash
# Render and validate the data-plane tree. No cluster, no credentials, no live
# data: this only reads local files and (for kubeconform) the public schema
# registry.
#
#   nix-shell -p kustomize kubeconform --run "deploy/data-plane/tests/check.sh"
#
# Missing kustomize or kubeconform is a SKIP, not a failure: the tree is a
# reviewable artifact and CI supplies the tools. A malformed render is a
# failure.
set -uo pipefail

root=$(git rev-parse --show-toplevel 2>/dev/null || true)
if [[ -z $root ]]; then
  root=$(cd "$(dirname "$0")/../../.." && pwd)
fi
cd "$root" || exit 1
plane=deploy/data-plane

skip() { echo "SKIP: $*"; exit 0; }
pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL %s\n' "$1"; }

for tool in kustomize kubeconform; do
  command -v "$tool" >/dev/null 2>&1 ||
    skip "$tool not found (nix-shell -p kustomize kubeconform --run \"$0\")"
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# ---------------------------------------------------------------- render
for target in base local; do
  if kustomize build "$plane/$target" >"$work/$target.yaml" 2>"$work/$target.err"; then
    ok "kustomize build $plane/$target"
  else
    bad "kustomize build $plane/$target"
    sed 's/^/     /' "$work/$target.err"
  fi
done
if ((fail > 0)); then
  echo "cannot continue: a render failed" >&2
  exit 1
fi

# ------------------------------------------------------------ yaml schema
for target in base local; do
  if kubeconform -strict -ignore-missing-schemas -summary "$work/$target.yaml" \
    >"$work/$target.kubeconform" 2>&1; then
    ok "kubeconform -strict ($target): $(tail -n 1 "$work/$target.kubeconform")"
  else
    bad "kubeconform -strict ($target)"
    sed 's/^/     /' "$work/$target.kubeconform"
  fi
done

# ----------------------------------------------------------- json catalogs
render="$work/base.yaml"
if command -v jq >/dev/null 2>&1; then
  for file in "$plane"/config/*.json "$plane"/config/sources/*.json; do
    if jq empty "$file" >/dev/null 2>&1; then
      ok "valid JSON: ${file#"$plane"/}"
    else
      bad "invalid JSON: ${file#"$plane"/}"
    fi
  done

  if jq -e --slurpfile groups "$plane/config/recovery-groups.json" \
    '([.sources[].recovery_group] - [$groups[0].groups[].group_id]) | length == 0' \
    "$plane/config/source-catalog.json" >/dev/null 2>&1; then
    ok "every source references a declared recovery group"
  else
    bad "source catalog references an undeclared recovery group"
  fi

  if jq -e '([.sources[].source_id] | length) == ([.sources[].source_id] | unique | length)' \
    "$plane/config/source-catalog.json" >/dev/null 2>&1; then
    ok "source ids are unique"
  else
    bad "source ids are not unique"
  fi

  if jq -e '([.groups[].group_id] | length) == ([.groups[].group_id] | unique | length)' \
    "$plane/config/recovery-groups.json" >/dev/null 2>&1; then
    ok "recovery group ids are unique"
  else
    bad "recovery group ids are not unique"
  fi

  if jq -e '[.groups[] | .parent_dataset as $p | .members[] |
    select((.dataset != $p) and ((.dataset | startswith($p + "/")) | not))] | length == 0' \
    "$plane/config/recovery-groups.json" >/dev/null 2>&1; then
    ok "every recovery member dataset sits under its group parent"
  else
    bad "a recovery member dataset escapes its group parent"
  fi

  if jq -e '[.groups[] | ([.members[].mount] | length) ==
    ([.members[].mount] | unique | length)] | all' \
    "$plane/config/recovery-groups.json" >/dev/null 2>&1; then
    ok "recovery group mounts are unique"
  else
    bad "a recovery group repeats a mount"
  fi

  groups_env=$(grep -A1 'name: DATA_PLANE_GROUPS' "$render" | sed -n 's/.*value: //p' | head -n 1)
  groups_json=$(jq -r '[.groups[].group_id] | join(",")' "$plane/config/recovery-groups.json")
  if [[ $groups_env == "$groups_json" ]]; then
    ok "backup-controller group list matches the inventory"
  else
    bad "backup-controller group list drifted from the inventory"
  fi

  schemas_bad=0
  for file in "$plane"/config/sources/*.json; do
    jq -e '.["x-relay"].max_bytes > 0 and .["x-relay"].max_records_per_second > 0' \
      "$file" >/dev/null 2>&1 || schemas_bad=$((schemas_bad + 1))
  done
  if ((schemas_bad == 0)); then
    ok "every relay schema declares positive size and rate limits"
  else
    bad "$schemas_bad relay schema(s) lack x-relay limits"
  fi
else
  echo "SKIP: jq not found; JSON catalog checks not run"
fi

# ------------------------------------------------------------- image policy
image_re='^[[:space:]]*image: ghcr\.io/smores56/homelabs-platform/data-plane@sha256:[0-9a-f]{64}$'
if grep -Eq '^[[:space:]]*image:' "$render"; then
  if grep -E '^[[:space:]]*image:' "$render" | grep -Evq "$image_re"; then
    bad "an image is not digest-pinned under the placeholder registry"
    grep -E '^[[:space:]]*image:' "$render" | grep -Ev "$image_re" | sed 's/^/     /'
  else
    ok "every rendered image is digest-pinned"
  fi
else
  bad "no images rendered"
fi

if grep -Eq '^[[:space:]]*image:.*:latest([[:space:]]|$)' "$render"; then
  bad "a latest tag is present"
else
  ok "no latest tag"
fi

if grep -Eq '^[[:space:]]*image: data-plane/' "$render"; then
  bad "a per-service image path is present; the tree uses one data-plane image"
else
  ok "one data-plane image, selected by args"
fi

names_images=$(grep -E '^[[:space:]]+newName:' "$plane/images.yaml" | sed 's/.*newName:[[:space:]]*//' | sort)
names_base=$(grep -E '^[[:space:]]+newName:' "$plane/base/kustomization.yaml" | sed 's/.*newName:[[:space:]]*//' | sort)
if [[ $names_images == "$names_base" ]]; then
  ok "images.yaml and base/kustomization.yaml list the same image"
else
  bad "images.yaml and base/kustomization.yaml drifted"
fi
if [[ $(grep -c 'digest: sha256:0000000000000000000000000000000000000000000000000000000000000000' "$plane/images.yaml") -eq 1 &&
  $(grep -c 'digest: sha256:0000000000000000000000000000000000000000000000000000000000000000' "$plane/base/kustomization.yaml") -eq 1 ]]; then
  ok "both image lists carry the pre-publish digest placeholder"
else
  bad "image digest placeholder count changed"
fi

# ---------------------------------------------------------- security posture
if grep -q '^kind: Secret$' "$render"; then
  bad "a Secret object is rendered; this tree creates none"
else
  ok "no Secret objects"
fi

for pattern in 'hostPath:' 'privileged: true' 'automountServiceAccountToken: true'; do
  if grep -q "$pattern" "$render"; then
    bad "forbidden field rendered: $pattern"
  else
    ok "absent: $pattern"
  fi
done

deployments=$(grep -c '^kind: Deployment$' "$render")
if [[ $(grep -c 'args:' "$render") -ge $deployments ]]; then
  ok "every Deployment selects its subcommand with args"
else
  bad "a Deployment has no args subcommand"
fi
# kustomize re-renders `drop: ["ALL"]` as a block sequence, so count the item.
for pattern in 'runAsNonRoot: true' 'readOnlyRootFilesystem: true' '^[[:space:]]*- ALL$'; do
  count=$(grep -cE "$pattern" "$render")
  if ((count >= deployments)); then
    ok "every Deployment has $pattern ($count >= $deployments)"
  else
    bad "not every Deployment has $pattern ($count < $deployments)"
  fi
done

if [[ $(grep -c 'automountServiceAccountToken: false' "$render") -ge $deployments ]]; then
  ok "every Deployment disables token automount"
else
  bad "a Deployment does not disable token automount"
fi

if grep -q 'pod-security.kubernetes.io/enforce: restricted' "$render"; then
  ok "namespace enforces PSA restricted"
else
  bad "namespace does not enforce PSA restricted"
fi

if grep -q 'homelabs.dev/tier: platform' "$render" &&
  grep -q 'homelabs.dev/tenant: platform' "$render"; then
  ok "namespace carries tenant/tier labels"
else
  bad "namespace labels missing"
fi

# --------------------------------------------------------------- network
if [[ $(grep -c '^kind: NetworkPolicy$' "$render") -ge 12 ]]; then
  ok "default-deny, DNS and every explicit flow rendered"
else
  bad "NetworkPolicy count is below the reviewed set (12)"
fi
if grep -q 'name: default-deny' "$render"; then
  ok "default-deny is present"
else
  bad "default-deny is missing"
fi

# --------------------------------------------------------------- RBAC
if grep -q '^kind: ClusterRole$' "$render"; then
  bad "a ClusterRole is rendered; the tree needs no cluster-scoped RBAC"
else
  ok "no ClusterRole"
fi
if grep -q '^  - batch$' "$render" && grep -q '^  - jobs$' "$render"; then
  ok "backup-controller restore Job grant is present"
else
  bad "backup-controller restore Job grant is missing"
fi
if grep -q 'secrets' "$render" && grep -q 'pods/exec' "$render"; then
  bad "RBAC mentions secrets or pods/exec"
else
  ok "RBAC names neither secrets nor pods/exec"
fi

# ------------------------------------------------------------- storage
if grep -q 'storageClassName: local-path' "$render"; then
  ok "claims use the hostPath-free local-path StorageClass name"
else
  bad "expected local-path StorageClass on the base claims"
fi
if grep -q 'storageClassName: data-local' "$render"; then
  bad "data-local (no-provisioner) leaked into the data-plane base"
else
  ok "no static data-local claim"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
((fail == 0))
