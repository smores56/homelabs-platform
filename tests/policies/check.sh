#!/usr/bin/env bash
# Policy checks for the platform admission, RBAC and network stack.
#
# Static mode (default, what CI runs): no cluster, no credentials. It asserts
# that every policy is paired with a binding, that each binding selects exactly
# the namespaces in bindings.tsv, that each fixture's expected policy really
# contains the CEL field the fixture violates, that RBAC grants nothing on the
# deny list, that the controller flags are the reviewed set, that NetworkPolicy
# coverage is complete, and that the stage ordering is the intended DAG.
#
# Runtime mode (--runtime): boots tests/policies/apiserver.sh (or uses
# POLICY_TEST_KUBECONFIG) and drives real admission and authorisation decisions
# for every fixture in fixtures/expectations.tsv and every row in rbac.tsv.
#
#   tests/policies/check.sh
#   tests/policies/check.sh --runtime
set -uo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root" || exit 1

runtime=0
external_kubeconfig="${POLICY_TEST_KUBECONFIG:-}"
for arg in "$@"; do
  case $arg in
    --runtime) runtime=1 ;;
    --kubeconfig=*) external_kubeconfig="${arg#*=}" ;;
    -h | --help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

pass=0
fail=0
ok() {
  pass=$((pass + 1))
  printf 'ok   %s\n' "$1"
}
bad() {
  fail=$((fail + 1))
  printf 'FAIL %s\n' "$1"
  [[ -n ${2:-} ]] && printf '     %s\n' "${2//$'\n'/$'\n'     }"
  return 0
}
eq() {
  if [[ $1 == "$2" ]]; then ok "$3"; else bad "$3" "got '$1' want '$2'"; fi
}

for tool in yq jq git kustomize; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "missing required tool: $tool" >&2
    echo "nix shell nixpkgs#yq-go nixpkgs#jq nixpkgs#kustomize -c tests/policies/check.sh" >&2
    exit 1
  }
done

work=$(mktemp -d)
apiserver_started=0
cleanup() {
  if ((apiserver_started)); then
    tests/policies/apiserver.sh stop >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

policies_dir=clusters/homelab/platform/policies
network_dir=clusters/homelab/platform/network
rbac_dir=clusters/homelab/platform/rbac
stages_dir=clusters/homelab/platform/kustomizations
fixtures_dir=tests/policies/fixtures

# yq converts one manifest file (possibly multi-document) into a JSON array so
# the rest of the checks can stay in jq.
json_of() { yq -o=json -I=0 '.' "$1" | jq -s '.'; }

if ! kustomize build clusters/homelab >"$work/render.yaml" 2>"$work/render.err"; then
  echo "kustomize build clusters/homelab failed:" >&2
  cat "$work/render.err" >&2
  exit 1
fi
json_of "$work/render.yaml" >"$work/render.json"
ok "kustomize build clusters/homelab renders"

# ---------------------------------------------------------------- YAML parsing
parse_failures=0
yaml_count=0
while IFS= read -r file; do
  case $file in
    clusters/* | tests/*) ;;
    *) continue ;;
  esac
  yaml_count=$((yaml_count + 1))
  if ! yq -o=json '.' "$file" >/dev/null 2>&1; then
    parse_failures=$((parse_failures + 1))
    bad "YAML parses: $file"
  fi
done < <(git ls-files '*.yaml' '*.yml')
if ((parse_failures == 0)); then
  ok "every clusters/ and tests/ YAML file parses (${yaml_count} files)"
fi

# ------------------------------------------------------- policy / binding pairs
policy_files=()
for file in "$policies_dir"/*.yaml; do
  [[ $file == */kustomization.yaml ]] && continue
  policy_files+=("$file")
done

for file in "${policy_files[@]}"; do
  json_of "$file" >"$work/policy.json"
  pcount=$(jq '[.[] | select(.kind == "ValidatingAdmissionPolicy")] | length' "$work/policy.json")
  bcount=$(jq '[.[] | select(.kind == "ValidatingAdmissionPolicyBinding")] | length' "$work/policy.json")
  name=$(jq -r '[.[] | select(.kind == "ValidatingAdmissionPolicy")][0].metadata.name' "$work/policy.json")
  bname=$(jq -r '[.[] | select(.kind == "ValidatingAdmissionPolicyBinding")][0].metadata.name' "$work/policy.json")
  bpolicy=$(jq -r '[.[] | select(.kind == "ValidatingAdmissionPolicyBinding")][0].spec.policyName' "$work/policy.json")
  fpolicy=$(jq -r '[.[] | select(.kind == "ValidatingAdmissionPolicy")][0].spec.failurePolicy' "$work/policy.json")
  expected_name=$(basename "$file" .yaml | sed -E 's/^[0-9]+-//')

  eq "${pcount}/${bcount}" "1/1" "paired policy+binding in $(basename "$file")"
  eq "$bpolicy" "$name" "binding references its policy ($(basename "$file"))"
  eq "$bname" "$name" "binding shares the policy name ($(basename "$file"))"
  eq "$name" "$expected_name" "policy name matches file name ($(basename "$file"))"
  eq "$fpolicy" "Fail" "policy fails closed ($name)"
done

# --------------------------------------------- bindings select intended objects
# Namespace labels come from both places that create namespaces: the platform
# namespaces stage and the bootstrap install manifests (flux-system, relabelled
# by clusters/homelab/kustomization.yaml).
kustomize build clusters/homelab/platform/namespaces >"$work/ns-stage.yaml"
json_of "$work/ns-stage.yaml" >"$work/ns-stage.json"
for source in "$work/ns-stage.json" "$work/render.json"; do
  jq '[.[] | select(.kind == "Namespace") | {name: .metadata.name, labels: (.metadata.labels // {})}]' \
    "$source"
done | jq -s 'add | unique_by(.name)' >"$work/namespaces.json"

selector_namespaces() {
  jq -r --argjson sel "$1" '
    def matches($l; $s):
      (((($s.matchLabels // {}) | to_entries) | all(. as $e | $l[$e.key] == $e.value)))
      and (((($s.matchExpressions // [])) | all(. as $x |
        if $x.operator == "In" then any($x.values[]; . == $l[$x.key])
        elif $x.operator == "NotIn" then (($l | has($x.key)) | not) or (all($x.values[]; . != $l[$x.key]))
        elif $x.operator == "Exists" then ($l | has($x.key))
        elif $x.operator == "DoesNotExist" then ($l | has($x.key) | not)
        else false end)));
    [.[] | select(matches(.labels; $sel)) | .name] | sort | join(",")
  ' "$work/namespaces.json"
}

while IFS=$'\t' read -r policy expected; do
  [[ -z $policy || $policy == \#* ]] && continue
  file=$(grep -l "^  name: ${policy}$" "$policies_dir"/*.yaml | head -1)
  if [[ -z $file ]]; then
    bad "binding selectors: policy $policy not found"
    continue
  fi
  selector=$(json_of "$file" | jq -c '[.[] | select(.kind == "ValidatingAdmissionPolicyBinding")][0].spec.matchResources.namespaceSelector')
  if [[ $selector == null ]]; then
    bad "binding $policy selects namespaces (cluster-wide selector missing)"
    continue
  fi
  eq "$(selector_namespaces "$selector")" "$expected" "binding $policy selects $expected"
done <tests/policies/bindings.tsv

# ------------------------------------------------- CEL mentions the tested field
while IFS=$'\t' read -r fixture _ns expect policy token; do
  [[ -z $fixture || $fixture == \#* ]] && continue
  [[ $expect == allow ]] && continue
  file=$(grep -l "^  name: ${policy}$" "$policies_dir"/*.yaml | head -1)
  if [[ -z $file ]]; then
    bad "CEL presence: policy $policy for $fixture not found"
    continue
  fi
  if grep -Fq -- "$token" "$file"; then
    ok "CEL presence: $policy covers $fixture"
  else
    bad "CEL presence: $policy does not mention '$token' ($fixture)"
  fi
done <"$fixtures_dir/expectations.tsv"

# ------------------------------------------------------------------------ RBAC
deny_resources='secrets|serviceaccounts|roles|rolebindings|clusterroles|clusterrolebindings|resourcequotas|limitranges|networkpolicies|ingresses|poddisruptionbudgets|customresourcedefinitions|namespaces|nodes|runtimeclasses|validatingadmissionpolicies|validatingadmissionpolicybindings|priorityclasses|storageclasses|persistentvolumes|kustomizations|gitrepositories|helmreleases|externalsecrets|secretstores|generators'
deny_verbs='escalate|bind|impersonate|approve|sign'
deny_groups='rbac.authorization.k8s.io|apiextensions.k8s.io|admissionregistration.k8s.io|toolkit.fluxcd.io|external-secrets.io'

rbac_json="$work/rbac.json"
: >"$rbac_json"
for file in "$rbac_dir"/*.yaml; do
  [[ $file == */kustomization.yaml ]] && continue
  jq -c '.[]' <<<"$(json_of "$file")" >>"$rbac_json"
done

eq "$(jq -r 'select(.kind == "ServiceAccount") | .metadata.namespace + ":" + .metadata.name' "$rbac_json" | sort | paste -sd, -)" \
  "calibre:data-workload,graphiti:data-workload,immich:data-workload,navidrome:data-workload,tenant-apps:agent-control,tenant-apps:nanobot,tenant-apps:tenant-apps-reconciler" \
  "the RBAC stage holds the tenant identities, the agent-control SA and one data-workload account per data namespace"
eq "$(jq -r 'select(.kind != "Role" and .kind != "RoleBinding" and .kind != "ServiceAccount") | .kind' "$rbac_json" | sort -u)" \
  "" "the RBAC stage holds only identities, Roles and RoleBindings"

hits=$(jq -r --arg re "^(${deny_resources})$" '
  . as $doc
  | if ($doc.kind == "Role" or $doc.kind == "ClusterRole") then
      ($doc.rules // [])[] | .resources[]? | select(test($re)) | "\($doc.metadata.name): resource \(.)"
    else empty end' "$rbac_json")
if [[ -z $hits ]]; then ok "no RBAC rule grants a denied resource"; else bad "RBAC grants denied resources" "$hits"; fi

hits=$(jq -r --arg re "^(${deny_verbs})$" '
  . as $doc
  | if ($doc.kind == "Role" or $doc.kind == "ClusterRole") then
      ($doc.rules // [])[] | .verbs[]? | select(test($re)) | "\($doc.metadata.name): verb \(.)"
    else empty end' "$rbac_json")
if [[ -z $hits ]]; then ok "no RBAC rule grants escalate/bind/impersonate"; else bad "RBAC grants escalation verbs" "$hits"; fi

hits=$(jq -r --arg re "(${deny_groups})" '
  . as $doc
  | if ($doc.kind == "Role" or $doc.kind == "ClusterRole") then
      ($doc.rules // [])[] | .apiGroups[]? | select(test($re)) | "\($doc.metadata.name): group \(.)"
    else empty end' "$rbac_json")
if [[ -z $hits ]]; then ok "no RBAC rule reaches an identity, CRD or Flux API group"; else bad "RBAC reaches a denied API group" "$hits"; fi

hits=$(jq -r '
  . as $doc
  | if ($doc.kind == "Role" or $doc.kind == "ClusterRole") then
      ($doc.rules // [])[] | (.resources[]?, .verbs[]) | select(. == "*") | "\($doc.metadata.name): wildcard"
    else empty end' "$rbac_json")
if [[ -z $hits ]]; then ok "no wildcard resources or verbs in RBAC"; else bad "RBAC uses a wildcard" "$hits"; fi

hits=$(jq -r 'select(.kind == "ClusterRole" or .kind == "ClusterRoleBinding") | .metadata.name' "$rbac_json")
if [[ -z $hits ]]; then ok "no cluster-scoped RBAC objects in the platform RBAC stage"; else bad "cluster-scoped RBAC objects present" "$hits"; fi

eq "$(jq -r 'select(.kind == "Role" and .metadata.name == "tenant-apps-reconciler") | .metadata.namespace' "$rbac_json")" \
  "tenant-apps" "tenant Role is namespace-scoped"
eq "$(jq -r 'select(.kind == "RoleBinding" and .metadata.name == "tenant-apps-reconciler") | .subjects | map("\(.kind):\(.namespace):\(.name)") | join(",")' "$rbac_json")" \
  "ServiceAccount:tenant-apps:tenant-apps-reconciler" "the tenant rolebinding binds only the tenant service account"
eq "$(jq -r 'select(.kind == "Role" and .metadata.name == "tenant-apps-reconciler") | [.rules[] | select(.resources | index("pods")) | .verbs | index("create")] | map(select(. != null)) | length' "$rbac_json")" \
  "1" "tenant Role can create pods (the tenant can still work)"

# The agent identity is its own account and grants exactly two reads and one
# write in each data namespace. Anything wider is a privilege change.
eq "$(jq -r 'select(.kind == "ServiceAccount" and .metadata.name == "nanobot") | [.metadata.namespace, (.automountServiceAccountToken // false)] | join(":")' "$rbac_json")" \
  "tenant-apps:false" "the nanobot ServiceAccount exists in tenant-apps with no token automount"
eq "$(jq -r 'select(.kind == "Role" and .metadata.name == "agent-debug") | .metadata.namespace' "$rbac_json" | sort | paste -sd, -)" \
  "calibre,graphiti,immich,navidrome" "agent-debug exists in every data namespace"
eq "$(jq -r 'select(.kind == "Role" and .metadata.name == "agent-debug") | [.rules[] | (.resources | join("+")) + ":" + (.verbs | sort | join("+"))] | sort | join(",")' "$rbac_json" | sort -u)" \
  "events:get+list,pods:delete" "agent-debug grants exactly events get/list and pods delete"
eq "$(jq -r 'select(.kind == "RoleBinding" and .metadata.name == "agent-debug") | .subjects | map("\(.kind):\(.namespace):\(.name)") | join(",")' "$rbac_json" | sort -u)" \
  "ServiceAccount:tenant-apps:nanobot" "agent-debug binds only the nanobot service account"

# ------------------------------------------------ agent-control (cluster API)
# The one identity that talks to the API on the agent's behalf. Every assertion
# here is a ceiling: the deployment's sandbox, the two RBAC halves, the single
# ingress path and the API egress are the whole capability (docs/tenancy.md).
control_dir=clusters/homelab/agent-control
kustomize build "$control_dir" >"$work/control.yaml"
json_of "$work/control.yaml" >"$work/control.json"

control_pod='.[] | select(.kind == "Deployment" and .metadata.name == "agent-control") | .spec.template.spec'
eq "$(jq -r "$control_pod | .serviceAccountName" "$work/control.json")" \
  "agent-control" "agent-control runs as its own service account"
eq "$(jq -r "$control_pod | if has(\"automountServiceAccountToken\") then .automountServiceAccountToken else true end" "$work/control.json")" \
  "true" "agent-control mounts the only tenant token in the cluster"
eq "$(jq -r "$control_pod | .runtimeClassName // \"none\"" "$work/control.json")" \
  "gvisor" "agent-control runs on gvisor"
eq "$(jq -r "$control_pod | .securityContext.runAsNonRoot" "$work/control.json")" \
  "true" "agent-control declares runAsNonRoot"
eq "$(jq -r "$control_pod | .containers[0].securityContext.readOnlyRootFilesystem" "$work/control.json")" \
  "true" "the agent-control rootfs is read-only"
eq "$(jq -r "$control_pod | .containers[0].securityContext.allowPrivilegeEscalation" "$work/control.json")" \
  "false" "agent-control forbids privilege escalation"
eq "$(jq -r "$control_pod | .containers[0].securityContext.capabilities.drop | join(\",\")" "$work/control.json")" \
  "ALL" "agent-control drops every capability"
eq "$(jq -r "$control_pod | [.containers[0].env[] | select(has(\"valueFrom\"))] | length" "$work/control.json")" \
  "0" "no agent-control env value comes from a Secret"
control_image=$(jq -r "$control_pod | .containers[0].image" "$work/control.json")
if [[ $control_image =~ @sha256:[0-9a-f]{64}$ ]]; then
  ok "the agent-control image is digest-pinned"
else
  bad "the agent-control image is not digest-pinned ($control_image)"
fi
# The base digest is shared with the broker's Dockerfile, so a bump lands in one
# review instead of two drifting pins. No tag: tag + digest is still a tag, and
# tenant-images rejects one.
base_digest=$(sed -n 's/^FROM [^ ]*\(@sha256:[0-9a-f]\{64\}\).*/\1/p' apps/broker/Dockerfile)
eq "${control_image#python}" "$base_digest" \
  "agent-control runs the broker's pinned python base"
eq "$(jq -r '.[] | select(.kind == "Service" and .metadata.name == "agent-control") | [.metadata.namespace, .spec.type, .spec.ports[0].port, .spec.ports[0].targetPort] | join(":")' "$work/control.json")" \
  "tenant-apps:ClusterIP:8081:http" "agent-control is a ClusterIP Service on 8081"
# The HTTP surface is the nine routes in ROUTES. A TCP probe means no health
# route can become a tenth, token-free endpoint.
eq "$(jq -r "$control_pod | .containers[0] | [(.startupProbe.tcpSocket.port // \"none\"), (.readinessProbe.tcpSocket.port // \"none\"), (.livenessProbe.tcpSocket.port // \"none\")] | join(\",\")" "$work/control.json")" \
  "http,http,http" "agent-control probes are TCP sockets, not HTTP routes"
if grep -q '"/healthz"' "$control_dir/agent_control.py"; then
  bad "the service exposes an HTTP health route outside the allowlist"
else
  ok "the service exposes no HTTP route outside the allowlist"
fi

eq "$(jq -r 'select(.kind == "Role" and .metadata.name == "agent-control") | [.metadata.namespace, ([.rules[] | (.resources | join("+")) + ":" + (.verbs | sort | join("+"))] | sort | join(","))] | join(":")' "$rbac_json")" \
  "tenant-apps:deployments:patch,pods+pods/log+events:get+list+watch" \
  "the agent-control Role grants pod/event reads and one deployment patch"
eq "$(jq -r 'select(.kind == "Role" and .metadata.name == "agent-control") | [.rules[] | select(.resources | index("deployments")) | (.resourceNames // []) | join(",")] | join(",")' "$rbac_json")" \
  "nanobot" "the patch rule is scoped to deployments/nanobot"
eq "$(jq -r 'select(.kind == "RoleBinding" and .metadata.name == "agent-control") | .subjects | map("\(.kind):\(.namespace):\(.name)") | join(",")' "$rbac_json")" \
  "ServiceAccount:tenant-apps:agent-control" "the agent-control RoleBinding binds only its own account"
eq "$(jq -r '.[] | select(.kind == "ClusterRole" and .metadata.name == "agent-control-read") | [.rules[] | (.apiGroups | join("+")) + "/" + (.resources | join("+")) + ":" + (.verbs | sort | join("+"))] | sort | join(",")' "$work/control.json")" \
  "/pods+events+nodes:get+list+watch,apps/deployments+statefulsets+daemonsets:get+list,kustomize.toolkit.fluxcd.io/kustomizations:get+list" \
  "the agent-control ClusterRole is read-only"
eq "$(jq -r '.[] | select(.kind == "ClusterRoleBinding" and .metadata.name == "agent-control-read") | .subjects | map("\(.kind):\(.namespace):\(.name)") | join(",")' "$work/control.json")" \
  "ServiceAccount:tenant-apps:agent-control" "the ClusterRoleBinding binds only the agent-control account"
if jq -r '.[] | select(.kind == "Role" or .kind == "ClusterRole") | .rules[] | .resources[]? | select(. == "secrets" or . == "configmaps")' \
  "$work/control.json" | grep -q . ||
  jq -r 'select(.kind == "Role" and .metadata.name == "agent-control") | .rules[] | .resources[]? | select(. == "secrets" or . == "configmaps")' \
    "$rbac_json" | grep -q .; then
  bad "agent-control RBAC reaches Secrets or ConfigMaps"
else
  ok "agent-control RBAC grants nothing on Secrets or ConfigMaps"
fi

eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "agent-control-from-broker") | [.metadata.namespace, .spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.ingress[0].from[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.ingress[0].ports[0].port] | join(":")' "$work/control.json")" \
  "tenant-apps:broker:broker:8081" "agent-control accepts ingress only from the broker on 8081"
eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "egress-agent-control") | [.metadata.namespace, .spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.egress[0].to[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.egress[0].ports[0].port] | join(":")' "$work/control.json")" \
  "broker:tenant-apps:agent-control:8081" "the broker's egress half is stated beside the ingress half"
api_egress='.[] | select(.kind == "NetworkPolicy" and .metadata.name == "agent-control-kubernetes-api-egress")'
eq "$(jq -r "$api_egress | [.spec.egress[0].to[] | .ipBlock.cidr] | sort | join(\",\")" "$work/control.json")" \
  "10.43.0.0/16,10.77.1.0/30" "agent-control API egress names the service range and the node tap"
eq "$(jq -r "$api_egress | [.spec.egress[0].ports[] | .port] | sort | unique | join(\",\")" "$work/control.json")" \
  "443,6443" "agent-control API egress covers both the ClusterIP and post-DNAT ports"
eq "$(jq -r "$api_egress | .spec.podSelector.matchLabels[\"app.kubernetes.io/name\"]" "$work/control.json")" \
  "agent-control" "the API egress policy selects only the agent-control pod"

# ------------------------------------------------------------ controller flags
declare -A controller_flags=(
  [kustomize-controller]="--feature-gates=ExternalArtifact=true,ObjectLevelWorkloadIdentity=true --no-cross-namespace-refs=true --no-remote-bases=true --default-service-account=default --default-decryption-service-account=default --default-kubeconfig-service-account=default"
  [helm-controller]="--feature-gates=ExternalArtifact=true,ObjectLevelWorkloadIdentity=true --no-cross-namespace-refs=true --default-service-account=default --default-kubeconfig-service-account=default"
  [notification-controller]="--feature-gates=ObjectLevelWorkloadIdentity=true --no-cross-namespace-refs=true --default-service-account=default"
  [source-controller]="--feature-gates=ObjectLevelWorkloadIdentity=true --default-service-account=default"
  [source-watcher]="--no-cross-namespace-refs=true"
  [image-reflector-controller]="--no-cross-namespace-refs=true"
  [image-automation-controller]="--no-cross-namespace-refs=true"
)

for controller in "${!controller_flags[@]}"; do
  args=$(jq -r --arg name "$controller" '
    .[] | select(.kind == "Deployment" and .metadata.name == $name)
    | .spec.template.spec.containers[0].args[]' "$work/render.json")
  if [[ -z $args ]]; then
    bad "controller $controller present in the render"
    continue
  fi
  for flag in ${controller_flags[$controller]}; do
    if grep -Fxq -- "$flag" <<<"$args"; then
      ok "$controller runs $flag"
    else
      bad "$controller is missing $flag"
    fi
  done
done

for file in clusters/homelab/flux-system/gotk-patches/*.json; do
  jq empty "$file" 2>/dev/null || bad "patch JSON parses: $file"
done
ok "every gotk patch file is valid JSON"

forbidden=$(grep -RIl --exclude-dir=.git -e '--insecure-kubeconfig-exec' -e '--insecure-kubeconfig-tls' \
  clusters scripts tests .github 2>/dev/null | grep -v -e '^tests/policies/check.sh$' -e '^tests/policies/fixtures/' || true)
if [[ -z $forbidden ]]; then
  ok "no insecure kubeconfig flags in manifests or scripts"
else
  bad "insecure kubeconfig flag present" "$forbidden"
fi

# ------------------------------------------------------- image automation
# The controllers are part of the install manifest; their objects and the write
# credential's contract are asserted separately so a widened policy or a
# checked-in Secret cannot pass review unnoticed.
kustomize build clusters/homelab/flux-system >"$work/flux-system.yaml"
json_of "$work/flux-system.yaml" >"$work/flux-system.json"

eq "$(jq -r '.[] | select(.kind == "ImageRepository" and .metadata.name == "nanobot") | [.metadata.namespace, .spec.image, .spec.interval] | join(":")' "$work/flux-system.json")" \
  "flux-system:ghcr.io/smores56/homelabs-platform/nanobot:5m" \
  "ImageRepository nanobot watches the platform registry path in flux-system"
eq "$(jq -r '.[] | select(.kind == "ImagePolicy" and .metadata.name == "nanobot") | [.metadata.namespace, .spec.imageRepositoryRef.name, .spec.filterTags.pattern, .spec.filterTags.extract, (.spec.policy.numerical.order // "")] | join(":")' "$work/flux-system.json")" \
  "flux-system:nanobot:^build-(?P<run>[0-9]+)$:\$run:asc" \
  "ImagePolicy nanobot orders the workflow's build-<n> tags numerically"
eq "$(jq -r '.[] | select(.kind == "ImageUpdateAutomation" and .metadata.name == "nanobot") | [.metadata.namespace, .spec.sourceRef.kind, .spec.sourceRef.name, .spec.git.checkout.ref.branch, .spec.git.push.branch, .spec.update.strategy, .spec.update.path] | join(":")' "$work/flux-system.json")" \
  "flux-system:GitRepository:homelabs-apps:main:main:Setters:./" \
  "ImageUpdateAutomation pushes to homelabs-apps main with the Setters strategy"
eq "$(jq -r '.[] | select(.kind == "GitRepository" and .metadata.namespace == "flux-system" and .metadata.name == "homelabs-apps") | [.spec.url, .spec.ref.branch, .spec.secretRef.name] | join(":")' "$work/flux-system.json")" \
  "ssh://git@ssh.github.com:443/smores56/homelabs-apps:main:homelabs-apps-write" \
  "the image automation source is homelabs-apps over SSH 443 with the write secret"
eq "$(jq -r '[.[] | select(.kind == "Secret")] | length' "$work/flux-system.json")" \
  "0" "the write credential is not in git (SOPS-managed Secret)"
if jq -r '.[] | select(.kind == "ConfigMap" and .metadata.name == "homelabs-apps-write-contract") | .data.marker' "$work/flux-system.json" |
  grep -Fq "{\"\$imagepolicy\": \"flux-system:nanobot\"}"; then
  ok "the write contract records the tenant-side setter marker"
else
  bad "the write contract does not record the setter marker"
fi
if [[ -f .github/workflows/publish-nanobot.yml ]] &&
  grep -Fq "tag=\"build-\${GITHUB_RUN_NUMBER}\"" .github/workflows/publish-nanobot.yml &&
  grep -Fq 'workflow_dispatch:' .github/workflows/publish-nanobot.yml &&
  grep -Fq 'GITHUB_STEP_SUMMARY' .github/workflows/publish-nanobot.yml; then
  ok "publish-nanobot builds build-<run_number> and publishes the digest"
else
  bad "publish-nanobot workflow is missing its tag or digest contract"
fi

# The image is deliberately barebones: a tool that is not in the Dockerfile and
# not in metadata.nanobot.requires.bins must fail at start, not drift.
if grep -Eq '^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$' images/nanobot/Dockerfile; then
  ok "the nanobot image base is digest-pinned"
else
  bad "the nanobot image base is not digest-pinned"
fi
if grep -v "^#" images/nanobot/Dockerfile | grep -Eqi "\b(jq|yq|ripgrep|rg|node|gh|sdlc)\b"; then
  bad "the nanobot image bakes a tool outside the bootstrap shell"
else
  ok "the nanobot image installs only the bootstrap shell"
fi
if grep -Eq '(exec nanobot "\$@")' images/nanobot/entrypoint.sh &&
  grep -Fq 'render.py' images/nanobot/entrypoint.sh; then
  ok "the entrypoint renders from NANOBOT_CONFIG_DIR then execs the pod args"
else
  bad "the entrypoint does not render-then-exec"
fi

# ------------------------------------------------ nanobot platform policy tree
# The prompt and model catalogue live in this repository and reach the pod as an
# exact-name ConfigMap; admission pins the mount, so the name cannot gain a hash
# suffix.
kustomize build clusters/homelab/platform/policy >"$work/nanobot-policy.yaml"
json_of "$work/nanobot-policy.yaml" >"$work/nanobot-policy.json"
eq "$(jq -r '.[] | select(.kind == "ConfigMap") | [.metadata.namespace, .metadata.name] | join(":")' "$work/nanobot-policy.json")" \
  "tenant-apps:nanobot-policy" "the platform renders nanobot-policy into tenant-apps with an exact name"
eq "$(jq -r '.[] | select(.kind == "ConfigMap") | .data | keys | sort | join(",")' "$work/nanobot-policy.json")" \
  "providers.yaml,system.md" "nanobot-policy holds only the prompt and the model catalogue"
for source in prompts/system.md models/providers.yaml; do
  key=${source##*/}
  first=$(head -1 "clusters/homelab/platform/policy/nanobot/$source")
  if jq -r --arg k "$key" '.[] | select(.kind == "ConfigMap") | .data[$k]' "$work/nanobot-policy.json" | grep -Fqx -- "$first"; then
    ok "nanobot-policy $key is rendered from cluster/homelab/platform/policy/nanobot/$source"
  else
    bad "nanobot-policy $key does not match its platform source"
  fi
done

# -------------------------------------------------------- NetworkPolicy shape
network_json="$work/network.json"
: >"$network_json"
for file in "$network_dir"/*.yaml; do
  [[ $file == */kustomization.yaml ]] && continue
  jq -c '.[]' <<<"$(json_of "$file")" >>"$network_json"
done

# The data tier is one namespace per app, so the same two rules must exist in
# every one of them: a missing default-deny is a wide-open app.
data_namespaces=(immich navidrome calibre graphiti)
for ns in tenant-apps broker ingress "${data_namespaces[@]}"; do
  types=$(jq -r --arg ns "$ns" 'select(.metadata.namespace == $ns and .metadata.name == "default-deny") | (.spec.policyTypes // []) | sort | join(",")' "$network_json")
  eq "$types" "Egress,Ingress" "default-deny covers both directions in $ns"
  dns=$(jq -r --arg ns "$ns" 'select(.metadata.namespace == $ns and .metadata.name == "allow-dns-egress") | .spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"]' "$network_json")
  eq "$dns" "kube-system" "DNS egress in $ns targets kube-system only"
done

eq "$(jq -r 'select(.metadata.namespace == "tenant-apps" and .metadata.name == "egress-broker-only") | [.spec.egress[0].to[0].namespaceSelector.matchLabels["homelabs.dev/tier"], .spec.egress[0].ports[0].port] | join(":")' "$network_json")" \
  "broker:8080" "tenant egress is broker-only on 8080"
eq "$(jq -r 'select(.metadata.namespace == "broker" and .metadata.name == "ingress-tenant-only") | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["homelabs.dev/tier"], .spec.ingress[0].ports[0].port] | join(":")' "$network_json")" \
  "tenant:8080" "broker ingress accepts tenants on 8080"

# The tenant's direct public path (#625). The `except` lists are the contract:
# every non-global range is denied on both families, every TCP/UDP port is
# admitted, and the policy cannot widen without this block changing.
tenant_public='select(.metadata.namespace == "tenant-apps" and .metadata.name == "egress-public")'
eq "$(jq -r "$tenant_public | [(.spec.podSelector | length | tostring), (.spec.policyTypes | join(\",\"))] | join(\":\")" "$network_json")" \
  "0:Egress" "tenant public egress applies to the whole namespace, outbound only"
eq "$(jq -r "$tenant_public | .spec.egress[0].to[0].ipBlock | [.cidr, (.except | sort | join(\",\"))] | join(\":\")" "$network_json")" \
  "0.0.0.0/0:10.0.0.0/8,100.64.0.0/10,127.0.0.0/8,169.254.0.0/16,172.16.0.0/12,192.168.0.0/16" \
  "tenant public egress goes to global unicast IPv4 only"
eq "$(jq -r "$tenant_public | .spec.egress[1].to[0].ipBlock | [.cidr, (.except | sort | join(\",\"))] | join(\":\")" "$network_json")" \
  "::/0:::1/128,fc00::/7,fe80::/10" "tenant public egress goes to global unicast IPv6 only"
eq "$(jq -r "$tenant_public | [.spec.egress[] | (.ports | map(.protocol) | sort | join(\"+\")) + \"@\" + ([.ports[] | select(has(\"port\"))] | length | tostring)] | join(\",\")" "$network_json")" \
  "TCP+UDP@0,TCP+UDP@0" "tenant public egress admits every TCP and UDP port and no other protocol"
# The broker is still reachable, and the tenant has no other flow: an added
# policy has to be added to this set, which is what keeps the egress reviewable.
eq "$(jq -s -r 'map(select(.metadata.namespace == "tenant-apps") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-tunnel-ingress,allow-webhook-ingress,default-deny,egress-broker-only,egress-public" \
  "tenant-apps holds exactly the DNS, tunnel, broker and public-egress allows"
while IFS=: read -r app port; do
  eq "$(jq -r --arg ns "$app" --arg name "$app-http-from-ingress" \
    'select(.metadata.namespace == $ns and .metadata.name == $name) | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.ingress[0].from[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.ingress[0].ports[0].port] | join(":")' "$network_json")" \
    "ingress::$port" "$app accepts HTTP from the ingress namespace only"
done <<'APPS'
immich:2283
navidrome:4533
calibre:8083
APPS

# Exact policy sets: an added flow has to be added to this assertion too, which
# is what keeps "only the intended flows" reviewable.
eq "$(jq -s -r 'map(select(.metadata.namespace == "immich") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-same-namespace,default-deny,immich-http-from-ingress,immich-ml-public-https,restic-egress" \
  "immich keeps only default-deny, DNS, same-namespace, ingress HTTP, model-download HTTPS and restic egress"
eq "$(jq -s -r 'map(select(.metadata.namespace == "navidrome") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-same-namespace,default-deny,navidrome-http-from-ingress,restic-egress" \
  "navidrome keeps only default-deny, DNS, same-namespace, ingress HTTP and restic egress"
eq "$(jq -s -r 'map(select(.metadata.namespace == "calibre") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-same-namespace,calibre-http-from-ingress,default-deny,restic-egress" \
  "calibre keeps only default-deny, DNS, same-namespace, ingress HTTP and restic egress"
eq "$(jq -s -r 'map(select(.metadata.namespace == "graphiti") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-same-namespace,default-deny,graphiti-public-https,memory-mcp-from-broker,memory-mcp-from-ingress,memory-mcp-nodeport-from-host,restic-egress" \
  "graphiti keeps only default-deny, DNS, same-namespace, Neuralwatt HTTPS, the broker, tunnel and host-tap paths to the wrapper, and restic egress"

eq "$(jq -r 'select(.metadata.namespace == "ingress" and .metadata.name == "egress-data-apps-only") | [.spec.egress[] | .to[0] | "\(.namespaceSelector.matchLabels["kubernetes.io/metadata.name"]):\(.podSelector.matchLabels["app.kubernetes.io/name"])"] | sort | join(",")' "$network_json")" \
  "calibre:calibre,graphiti:memory-mcp,immich:immich,navidrome:navidrome" \
  "the ingress namespace reaches the four published app Services and nothing else"
eq "$(jq -r 'select(.metadata.namespace == "ingress" and .metadata.name == "egress-tunnel-edge") | [.spec.podSelector.matchLabels["app.kubernetes.io/name"], (.spec.egress[0].ports | map("\(.protocol):\(.port)") | sort | join(","))] | join(":")' "$network_json")" \
  "cloudflared:TCP:443,TCP:7844,UDP:7844" "cloudflared reaches the Cloudflare edge on 443 and the tunnel's 7844"
eq "$(jq -r 'select(.metadata.namespace == "ingress" and .metadata.name == "egress-agent") | [.spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.egress[0].to[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.egress[0].ports[0].port] | join(":")' "$network_json")" \
  "tenant-apps:nanobot:8765" "the tunnel reaches only the nanobot pod on the WebUI port"
eq "$(jq -r 'select(.metadata.namespace == "tenant-apps" and .metadata.name == "allow-tunnel-ingress") | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.ingress[0].from[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.ingress[0].ports[0].port] | join(":")' "$network_json")" \
  "ingress:cloudflared:8765" "the tenant admits only the tunnel on the WebUI port"

# The memory cutover (SDLC #617): the cluster tunnel is the new path in, and the
# host tap's NodePort path stays until the host route is retired elsewhere.
eq "$(jq -r 'select(.metadata.namespace == "graphiti" and .metadata.name == "memory-mcp-from-ingress") | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.ingress[0].from[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.ingress[0].ports[0].port] | join(":")' "$network_json")" \
  "ingress:cloudflared:8182" "the tunnel reaches the memory wrapper on its MCP port"
eq "$(jq -r 'select(.metadata.namespace == "graphiti" and .metadata.name == "memory-mcp-nodeport-from-host") | [(.spec.ingress[0].from | map(.ipBlock.cidr) | sort | join(",")), (.spec.ingress[0].ports[0].port | tostring)] | join(":")' "$network_json")" \
  "10.77.1.1/32,10.77.1.2/32:8182" "the host tap path to the wrapper is untouched"
# The broker's own path (#631): its credential is the boundary, the rule is the
# network half, and it names the broker namespace rather than a tier.
eq "$(jq -r 'select(.metadata.namespace == "graphiti" and .metadata.name == "memory-mcp-from-broker") | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], (.spec.ingress[0].from[0].podSelector // {} | length | tostring), .spec.ingress[0].ports[0].port] | join(":")' "$network_json")" \
  "broker:0:8182" "the broker reaches the memory wrapper on its MCP port and nothing else"

# ------------------------------------------------------- data storage contract
storage_dir=clusters/homelab/platform/storage
sc="$storage_dir/storageclass.yaml"
pvs="$storage_dir/persistentvolumes.yaml"

eq "$(yq -r '.provisioner' "$sc")" "kubernetes.io/no-provisioner" "data-local has no provisioner"
eq "$(yq -r '.reclaimPolicy' "$sc")" "Retain" "data-local retains volumes"
eq "$(yq -r '.volumeBindingMode' "$sc")" "WaitForFirstConsumer" "data-local binds in the consuming pod's node"
eq "$(yq -r '.allowVolumeExpansion' "$sc")" "false" "data-local is not expandable"

eq "$(yq -r '.spec.local.path' "$pvs" | grep -v '^---$' | sort | paste -sd, -)" \
  "/mnt/data/books,/mnt/data/books,/mnt/data/cluster-digest,/mnt/data/graphiti,/mnt/data/immich,/mnt/data/music" \
  "every PV path is one of the guest's virtiofs shares"
eq "$(yq -r '.spec.storageClassName' "$pvs" | grep -v '^---$' | sort -u)" "data-local" "every PV claims data-local"
eq "$(yq -r '.spec.persistentVolumeReclaimPolicy' "$pvs" | grep -v '^---$' | sort -u)" "Retain" "every PV retains its data"
eq "$(yq -r '.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].key' "$pvs" | grep -v '^---$' | sort -u)" \
  "kubernetes.io/hostname" "every PV pins node affinity"
eq "$(yq -r '.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]' "$pvs" | grep -v '^---$' | sort -u)" \
  "cluster" "every PV is bound to the cluster node"
eq "$(yq -r '.spec.claimRef | [.namespace, .name] | join(":")' "$pvs" | grep -v '^---$' | sort | paste -sd, -)" \
  "calibre:calibre-data,graphiti:graphiti-data,immich:immich-data,navidrome:navidrome-data,observability:cluster-digest-data,tenant-apps:nanobot-books" \
  "each PV is pre-bound to exactly one claim"
if grep -rq 'hostPath' clusters/homelab/data clusters/homelab/ingress; then
  bad "a data or ingress manifest uses hostPath"
else
  ok "no data or ingress manifest uses hostPath"
fi

for ns in "${data_namespaces[@]}"; do
  eq "$(yq -r '[.metadata.namespace, .metadata.name, .spec.storageClassName] | join(":")' \
    "clusters/homelab/data/$ns/storage.yaml")" "$ns:$ns-data:data-local" \
    "$ns has exactly one PVC named $ns-data on data-local"
done

# ------------------------------------------------------------- dump contract
# The dumps must be on the share before the host's restic walk (03:30
# Europe/Berlin, up to 03:45 with its random delay), so the schedule, the
# timezone and the deadline are all asserted, not just the path.
while IFS=: read -r app suffix; do
  cj="clusters/homelab/data/$app/dump-cronjob.yaml"
  eq "$(yq -r '.spec.schedule' "$cj")" "45 2 * * *" "$app dump runs at 02:45"
  eq "$(yq -r '.spec.timeZone' "$cj")" "Europe/Berlin" "$app dump uses the host's timezone"
  eq "$(yq -r '.spec.concurrencyPolicy' "$cj")" "Forbid" "$app dump never overlaps itself"
  eq "$(yq -r '.spec.jobTemplate.spec.activeDeadlineSeconds' "$cj")" "1800" \
    "$app dump is bounded to 30 minutes, before restic"
  eq "$(yq -r '[.spec.jobTemplate.spec.template.spec.volumes[] | select(.persistentVolumeClaim) | .persistentVolumeClaim.claimName] | join(",")' "$cj")" \
    "$app-data" "$app dump writes into the app's own claim"
  if grep -Fq "/data/db-dumps/$app.$suffix" "$cj"; then
    ok "$app dump writes /data/db-dumps/$app.$suffix"
  else
    bad "$app dump does not write /data/db-dumps/$app.$suffix"
  fi
done <<'DUMPS'
immich:dump
graphiti:rdb
DUMPS

# ------------------------------------------------- per-tree restic contract
# One job per tree, in the namespace that owns the claim (#619). What makes a
# per-tree job safe is asserted here: its own claim and nothing else, mounted
# read-only, uid 1000, its own repository prefix under the shared bucket, and a
# preflight that prints the drill which gates the host timer's retirement.
account_id=$(sed -nE 's/^[[:space:]]*account_id[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' tofu/terraform.tfvars)
while IFS=: read -r app deadline; do
  cj="clusters/homelab/data/$app/restic-backup.yaml"
  pre="clusters/homelab/data/$app/restic-preflight.yaml"
  contract="clusters/homelab/data/$app/restic-contract.yaml"
  eq "$(yq -r '[.spec.schedule, .spec.timeZone, .spec.concurrencyPolicy, .spec.jobTemplate.spec.activeDeadlineSeconds] | join(":")' "$cj")" \
    "0 5 * * *:Europe/Berlin:Forbid:$deadline" "$app restic runs after the dumps, bounded and serial"
  eq "$(yq -r '[.spec.jobTemplate.spec.template.spec.volumes[] | select(.persistentVolumeClaim) | (.persistentVolumeClaim.claimName + ":" + (.persistentVolumeClaim.readOnly | tostring))] | join(",")' "$cj")" \
    "$app-data:true" "$app restic mounts only its own claim, read-only"
  eq "$(yq -r '.spec.jobTemplate.spec.template.spec.securityContext.runAsUser' "$cj")" "1000" \
    "$app restic runs as the share's uid"
  eq "$(yq -r '[.spec.jobTemplate.spec.template.spec.containers[0].env[] | select(.valueFrom) | .name] | sort | join(",")' "$cj")" \
    "AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY,RESTIC_PASSWORD" \
    "$app restic takes its key and password from the projected Secret only"
  repository="s3:https://$account_id.r2.cloudflarestorage.com/restic-backup/trees/$app"
  eq "$(yq -r '.spec.jobTemplate.spec.template.spec.containers[0].env[] | select(.name == "RESTIC_REPOSITORY") | .value' "$cj")" \
    "$repository" "$app restic writes its own prefix of the shared bucket"
  eq "$(yq -r '.data.repository' "$contract")" "$repository" \
    "$app contract and job name the same repository"
  eq "$(yq -r '[.spec.schedule, .spec.timeZone, .spec.jobTemplate.spec.backoffLimit] | join(":")' "$pre")" \
    "0 12 * * 0:Europe/Berlin:0" "$app preflight runs weekly and does not retry into a green run"
  eq "$(yq -r '[.spec.jobTemplate.spec.template.spec.volumes[] | select(.configMap) | .configMap.name] | join(",")' "$pre")" \
    "restic-contract" "$app preflight prints the drill from the contract ConfigMap"
  eq "$(yq -r 'select(.kind == "ExternalSecret") | [.metadata.namespace, .spec.target.name, .spec.data[0].secretKey] | join(":")' \
    "clusters/homelab/platform/secrets/eso/restic-backup-$app.yaml" | grep -v '^$')" \
    "$app:restic-credentials:AWS_ACCESS_KEY_ID" "$app projects restic-credentials into its own namespace"
done <<'TREES'
immich:3600
navidrome:1800
calibre:1800
graphiti:1800
TREES
if grep -ho 'docker.io/restic/restic:0.18.1@sha256:[0-9a-f]\{64\}' \
  clusters/homelab/data/*/restic-backup.yaml clusters/homelab/data/*/restic-preflight.yaml |
  grep -c . | grep -qx 8; then
  ok "every restic job pins the image digest"
else
  bad "every restic job pins the image digest"
fi

# ------------------------------------------------- data workload manifest shape
# Admission (data-workload-source) enforces the identity at runtime; this is the
# pre-merge half, so a manifest that would be denied never reaches review.
kustomize build clusters/homelab/data >"$work/data-apps.yaml"
json_of "$work/data-apps.yaml" >"$work/data-apps.json"

jq_workloads='
  def podspec:
    if .kind == "CronJob" then .spec.jobTemplate.spec.template.spec
    elif (.kind == "Deployment" or .kind == "StatefulSet") then .spec.template.spec
    else null end;
  def podmeta:
    if .kind == "CronJob" then .spec.jobTemplate.spec.template.metadata
    elif (.kind == "Deployment" or .kind == "StatefulSet") then .spec.template.metadata
    else null end;
  .[] | select(podspec != null)
'
eq "$(jq -r "$jq_workloads | podspec.serviceAccountName // \"none\"" "$work/data-apps.json" | sort -u)" \
  "data-workload" "every data workload runs as the data-workload service account"
eq "$(jq -r "$jq_workloads | podmeta.labels[\"homelabs.dev/managed-by\"] // \"none\"" "$work/data-apps.json" | sort -u)" \
  "platform" "every data workload carries homelabs.dev/managed-by=platform"
eq "$(jq -r "$jq_workloads | select((podspec.volumes // []) | any(.persistentVolumeClaim != null)) | select(podspec.securityContext.runAsUser != 1000) | .metadata.name" "$work/data-apps.json")" \
  "" "every data pod that mounts a share runs as uid 1000 (the share's host owner through the guest translation)"
# Graphiti mounts no share (FalkorDB owns the graph volume) and its image runs `uv`
# from /root/.local/bin, so it starts as root. Allow exactly that one, so a new
# root workload cannot appear unnoticed.
eq "$(jq -r "$jq_workloads | select(podspec.securityContext.runAsUser == 0) | .metadata.name" "$work/data-apps.json" | sort -u)" \
  "graphiti" "graphiti is the only data workload that starts as root"
eq "$(jq -r "$jq_workloads | podspec.containers[] | select((.resources.limits // {}) == {}) | .name" "$work/data-apps.json")" \
  "" "every data container sets resource limits"
# Probes are for long-running containers; a dump job's own exit code is its
# liveness signal.
eq "$(jq -r '.[] | select(.kind == "Deployment" or .kind == "StatefulSet") | .spec.template.spec.containers[] | select((.readinessProbe // .livenessProbe // .startupProbe) == null) | .name' "$work/data-apps.json")" \
  "" "every long-running data container has at least one probe"

# The memory wrapper is reachable only through the host's tap forwarder: a
# NodePort whose literal must match ports.memoryNodePort in smores56/nix-config,
# and no Ingress anywhere in the namespace (memory.sammohr.dev stays on the host
# tunnel).
eq "$(jq -r '.[] | select(.kind == "Service" and .metadata.name == "memory-mcp") | "\(.spec.type):\(.spec.ports[0].nodePort):\(.spec.ports[0].port)"' "$work/data-apps.json")" \
  "NodePort:30082:8182" "the wrapper Service is the host tap's NodePort on 8182"
eq "$(jq -r '[.[] | select(.kind == "Ingress" and .metadata.namespace == "graphiti")] | length' "$work/data-apps.json")" \
  "0" "graphiti publishes no hostname; the wrapper's route stays on the host tunnel"
eq "$(jq -r '.[] | select(.kind == "Deployment" and .metadata.name == "memory-mcp") | "\(.spec.replicas):\(.spec.strategy.type)"' "$work/data-apps.json")" \
  "1:Recreate" "one wrapper replica, Recreate (single-writer SQLite on the share)"
eq "$(jq -r '.[] | select(.kind == "Deployment" and .metadata.name == "memory-mcp") | .spec.template.spec.containers[0].env[] | select(.name == "MEMORY_MCP_TOKEN_FILE") | .value' "$work/data-apps.json")" \
  "/secrets/token" "the wrapper reads its bearer token from the projected Secret key"
# A Secret volume mounts keys, not the Secret's name. If the ExternalSecret's
# target key and the filename the wrapper opens drift apart, every request 503s
# while the pod stays Ready (the /health probe is token-exempt).
token_file=$(jq -r '.[] | select(.kind == "Deployment" and .metadata.name == "memory-mcp") | .spec.template.spec.containers[0].env[] | select(.name == "MEMORY_MCP_TOKEN_FILE") | .value' "$work/data-apps.json")
eq "${token_file##*/}" \
  "$(yq -r '.spec.data[0].secretKey' clusters/homelab/platform/secrets/eso/graphiti-memory-token.yaml)" \
  "the wrapper opens the key the ExternalSecret projects"
eq "$(jq -r '.[] | select(.kind == "Deployment" and .metadata.name == "memory-mcp") | .spec.template.spec.containers[0].env[] | select(.name == "MEMORY_LLM_URL") | .value' "$work/data-apps.json")" \
  "" "the wrapper has no model gateway to probe in-cluster"
# An absent variable would silently restore the 127.0.0.1:8082 default and re-arm
# the model gate on an unreachable gateway, so the empty value must be explicit.
eq "$(jq -r '.[] | select(.kind == "Deployment" and .metadata.name == "memory-mcp") | [.spec.template.spec.containers[0].env[] | select(.name == "MEMORY_LLM_URL")] | length' "$work/data-apps.json")" \
  "1" "the wrapper's env names MEMORY_LLM_URL explicitly"

# Both memory manifests must carry the digest `just publish-memory-image`
# printed, never the zero placeholder that would fail the pull.
eq "$(grep -ho 'ghcr.io/smores56/nix-config/memory-mcp@sha256:[0-9a-f]*' \
  clusters/homelab/data/graphiti/memory-mcp.yaml \
  clusters/homelab/data/graphiti/memory-export-cronjob.yaml | grep -vc ':0\{64\}$')" \
  "2" "both memory manifests pin the published image digest"

# The export has its own contract: it precedes the 02:45 graph dump (both trigger
# an RDB save on the same FalkorDB) and the host's 03:30 restic walk.
eq "$(yq -r '[.spec.schedule, .spec.timeZone, .spec.concurrencyPolicy, .spec.jobTemplate.spec.activeDeadlineSeconds] | join(":")' \
  clusters/homelab/data/graphiti/memory-export-cronjob.yaml)" \
  "20 2 * * *:Europe/Berlin:Forbid:900" "memory export runs ahead of the graph dump and restic"
eq "$(yq -r '[.spec.jobTemplate.spec.template.spec.volumes[] | select(.persistentVolumeClaim) | .persistentVolumeClaim.claimName] | join(",")' \
  clusters/homelab/data/graphiti/memory-export-cronjob.yaml)" \
  "graphiti-data" "memory export writes into the app's own claim"

# --------------------------------------------------------- hostname contract
while IFS=: read -r ns host svc port; do
  file="clusters/homelab/data/$ns/ingress.yaml"
  eq "$(yq -r '.spec.rules[0].host' "$file")" "$host" "$host is declared in $ns"
  eq "$(yq -r '.spec.rules[0].http.paths[0].backend.service.name' "$file")" "$svc" \
    "$host routes to the $svc Service"
  eq "$(yq -r '.spec.rules[0].http.paths[0].backend.service.port.number' "$file")" "$port" \
    "$host routes to port $port"
done <<'INGRESS'
immich:photos.sammohr.dev:immich-server:2283
navidrome:music.sammohr.dev:navidrome:4533
calibre:opds.sammohr.dev:calibre:8083
INGRESS

# ---------------------------------------------------------- broker contract
broker_dir=clusters/homelab/broker
kustomize build "$broker_dir" >"$work/broker.yaml"
json_of "$work/broker.yaml" >"$work/broker.json"

podspec='.[] | select(.kind == "Deployment" and .metadata.name == "broker") | .spec.template.spec'
eq "$(jq -r "$podspec | .runtimeClassName // \"none\"" "$work/broker.json")" "gvisor" "the broker runs on gvisor"
eq "$(jq -r "$podspec | if has(\"automountServiceAccountToken\") then .automountServiceAccountToken else true end" "$work/broker.json")" "false" \
  "the broker never mounts a service-account token"
eq "$(jq -r "$podspec | .securityContext.runAsNonRoot" "$work/broker.json")" "true" \
  "the broker pod declares runAsNonRoot"
eq "$(jq -r "$podspec | .serviceAccountName" "$work/broker.json")" "broker" \
  "the broker runs as its own service account"
eq "$(jq -r "$podspec | .containers[0].securityContext.readOnlyRootFilesystem" "$work/broker.json")" "true" \
  "the broker rootfs is read-only"
eq "$(jq -r "$podspec | .containers[0].securityContext.allowPrivilegeEscalation" "$work/broker.json")" "false" \
  "the broker forbids privilege escalation"
eq "$(jq -r "$podspec | .containers[0].securityContext.capabilities.drop | join(\",\")" "$work/broker.json")" "ALL" \
  "the broker drops every capability"
eq "$(jq -r "$podspec | [.containers[0].env[] | select(has(\"valueFrom\"))] | length" "$work/broker.json")" "0" \
  "no broker env value comes from a Secret (credentials are files)"
eq "$(jq -r "$podspec | [.volumes[] | select(.name == \"credentials\")] | .[0] | [.secret.secretName, (.secret.optional // false)] | join(\":\")" "$work/broker.json")" \
  "broker-credentials:false" "the broker credential volume is fail-closed"
eq "$(jq -r '.[] | select(.kind == "Secret") | .metadata.name' "$work/broker.json")" "" \
  "the broker build contains no Secret"
eq "$(jq -r '.[] | select(.kind == "ServiceAccount" and .metadata.name == "broker") | .automountServiceAccountToken' "$work/broker.json")" \
  "false" "the broker ServiceAccount has no token automount"
eq "$(jq -r '.[] | select(.kind == "Service" and .metadata.name == "broker") | [.spec.ports[0].port, .spec.ports[0].targetPort] | join(":")' "$work/broker.json")" \
  "8080:http" "the broker Service exposes 8080 to the http port"

bimage=$(jq -r "$podspec | .containers[0].image" "$work/broker.json")
case $bimage in
  ghcr.io/smores56/homelabs-platform/broker@sha256:*) ok "broker image is the platform repository" ;;
  *) bad "broker image is not the platform repository ($bimage)" ;;
esac
if [[ $bimage =~ @sha256:[0-9a-f]{64}$ ]]; then
  ok "broker image is digest-pinned"
else
  bad "broker image is not digest-pinned ($bimage)"
fi

# The enrollment file is data, not a credential. Its shape is the contract the
# agent's BROKER_URL paths depend on; the unit tests parse the same file.
enrollment='.[] | select(.kind == "ConfigMap" and (.metadata.name | startswith("broker-config"))) | .data["broker.json"] | fromjson'
eq "$(jq -r "$enrollment | .logical_hosts | keys | sort | join(\",\")" "$work/broker.json")" \
  "github-agent,github-agent-push,github-apps,github-platform,github-platform-push,github-sdlc,github-sdlc-git,loki,memory,neuralwatt,notify,self,status,tinyfish" \
  "the broker enrolls exactly the agent's logical hosts"
eq "$(jq -r "$enrollment | [.logical_hosts | to_entries[] | select(.value.allow_private) | .key] | sort | join(\",\")" "$work/broker.json")" \
  "loki,memory,self,status" "only the in-cluster hosts are private enrollments"
eq "$(jq -r "$enrollment | .logical_hosts.notify | [.host, .allow_private, .inject.kind, .secret, (.optional_secret | tostring), (.allow_methods | join(\",\")), (.allow_paths | join(\";\")), (.max_body_bytes | tostring)] | join(\":\")" "$work/broker.json")" \
  "notify.invalid:false:bearer:notify-token:true:POST:^/v1/notify$:4096" \
  "notify is a public placeholder, POST-only, bearer, body-capped, optional token"
eq "$(jq -r "$enrollment | .logical_hosts.self | [.host, .port, .allow_private, (.secret // \"-\"), (.inject.kind // \"none\")] | join(\":\")" "$work/broker.json")" \
  "agent-control.tenant-apps.svc.cluster.local:8081:true:-:none" \
  "self is a credential-free private enrollment"
eq "$(jq -r "$enrollment | .logical_hosts.memory | [.scheme, .host, .port, .allow_private, .inject.kind, .secret, (.optional_secret | tostring)] | join(\":\")" "$work/broker.json")" \
  "http:memory-mcp.graphiti.svc.cluster.local:8182:true:bearer:memory-mcp-token:true" \
  "memory is the in-cluster wrapper on 8182 with an optional bearer token"
eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "egress-agent-control") | [.spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.egress[0].ports[0].port] | join(":")' "$work/broker.json")" \
  "tenant-apps:8081" "broker egress admits agent-control on 8081 only"
eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "egress-memory") | [.spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.egress[0].to[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.egress[0].ports[0].port] | join(":")' "$work/broker.json")" \
  "graphiti:memory-mcp:8182" "broker egress admits the memory wrapper on 8182 only"
eq "$(jq -r "$enrollment | .logical_hosts.loki.headers[\"X-Scope-OrgID\"]" "$work/broker.json")" "data" \
  "the loki logical host pins the data tenant header"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-apps\"].inject.kind" "$work/broker.json")" "basic" \
  "github-apps injects git basic auth"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-platform\"].host" "$work/broker.json")" "api.github.com" \
  "github-platform is the REST API host"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-platform-push\"].host" "$work/broker.json")" "github.com" \
  "github-platform-push is the git transport host"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-apps\"].github_app.permissions.contents" "$work/broker.json")" "write" \
  "github-apps may write contents"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-platform\"].github_app.permissions | keys | sort | join(\",\")" "$work/broker.json")" \
  "contents,pull_requests" "github-platform asks only for contents and pull requests"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-agent-push\"].host" "$work/broker.json")" "github.com" \
  "github-agent-push is the git transport host for the agent's repos"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-agent\"].host" "$work/broker.json")" "api.github.com" \
  "github-agent is the REST API host for the agent's repos"
eq "$(jq -r "$enrollment | [.logical_hosts[\"github-agent-push\"], .logical_hosts[\"github-agent\"]] | map(.github_app.repositories | sort | join(\",\")) | join(\";\")" "$work/broker.json")" \
  "kanbei,nix-config;kanbei,nix-config" \
  "both agent hosts cover the same two repositories"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-agent\"].github_app.permissions | keys | sort | join(\",\")" "$work/broker.json")" \
  "contents,pull_requests" "github-agent asks only for contents and pull requests"
eq "$(jq -r "$enrollment | .logical_hosts[\"github-agent-push\"].github_app.permissions | keys | sort | join(\",\")" "$work/broker.json")" \
  "contents,workflows" "github-agent-push asks only for contents and workflows"
eq "$(jq -r "$enrollment | [.logical_hosts[] | .secret // empty] | unique | sort | join(\",\")" "$work/broker.json")" \
  "$(jq -r '.[] | select(.kind == "ConfigMap" and .metadata.name == "broker-credentials-contract") | .data.keys | split(",") | sort | join(",")' "$work/broker.json")" \
  "the SOPS contract names exactly the enrolled secrets"
eq "$(jq -r "$enrollment | [.github_app.key_secret, .github_app.id_secret] + [.logical_hosts[] | .github_app.installation // empty] | unique | sort | join(\",\")" "$work/broker.json")" \
  "$(jq -r '.[] | select(.kind == "ConfigMap" and .metadata.name == "broker-credentials-contract") | .data.app_keys | split(",") | sort | join(",")' "$work/broker.json")" \
  "the SOPS contract names exactly the app credential files"
broker_config=$(jq -r "$enrollment | tostring" "$work/broker.json")
if grep -Eq 'ghp_[A-Za-z0-9]{20,}|github_pat_|sk-[A-Za-z0-9]{20,}|xox[baprs]-' <<<"$broker_config"; then
  bad "the broker enrollment file contains a token-shaped value"
else
  ok "the broker enrollment file contains no credential value"
fi

# Coarse egress: public 443 only. The broker's own address check is asserted by
# the unit tests; this is the CNI layer that must not be wider.
eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "egress-public-https") | [.spec.egress[0].to[0].ipBlock.cidr, .spec.egress[0].ports[0].port] | join(":")' "$work/broker.json")" \
  "0.0.0.0/0:443" "broker egress is public TCP 443 only"
eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "egress-public-https") | .spec.egress[0].to[0].ipBlock.except | sort | join(",")' "$work/broker.json")" \
  "0.0.0.0/8,10.0.0.0/8,100.64.0.0/10,127.0.0.0/8,169.254.0.0/16,172.16.0.0/12,192.0.0.0/24,192.168.0.0/16,198.18.0.0/15,224.0.0.0/4,240.0.0.0/4" \
  "broker egress excludes every non-global IPv4 range"

# ---------------------------------------------------------------- webhook contract
# The receiver is the one public ingress to an agent-facing service: its
# capability store, its tokens, its single egress and its runtime are asserted
# here so a widened deployment never reaches review.
webhook_dir=clusters/homelab/webhook
kustomize build "$webhook_dir" >"$work/webhook.yaml"
json_of "$work/webhook.yaml" >"$work/webhook.json"

wpod='.[] | select(.kind == "Deployment" and .metadata.name == "webhook") | .spec.template.spec'
eq "$(jq -r "$wpod | .runtimeClassName // \"none\"" "$work/webhook.json")" "gvisor" \
  "the webhook runs on gvisor"
eq "$(jq -r "$wpod | if has(\"automountServiceAccountToken\") then .automountServiceAccountToken else true end" "$work/webhook.json")" "false" \
  "the webhook never mounts a service-account token"
eq "$(jq -r "$wpod | .securityContext.runAsNonRoot" "$work/webhook.json")" "true" \
  "the webhook pod declares runAsNonRoot"
eq "$(jq -r "$wpod | .serviceAccountName" "$work/webhook.json")" "webhook" \
  "the webhook runs as its own service account"
eq "$(jq -r "$wpod | .containers[0].securityContext.readOnlyRootFilesystem" "$work/webhook.json")" "true" \
  "the webhook rootfs is read-only"
eq "$(jq -r "$wpod | .containers[0].securityContext.allowPrivilegeEscalation" "$work/webhook.json")" "false" \
  "the webhook forbids privilege escalation"
eq "$(jq -r "$wpod | .containers[0].securityContext.capabilities.drop | join(\",\")" "$work/webhook.json")" "ALL" \
  "the webhook drops every capability"
eq "$(jq -r '.[] | select(.kind == "Deployment" and .metadata.name == "webhook") | "\(.spec.replicas):\(.spec.strategy.type)"' "$work/webhook.json")" \
  "1:Recreate" "one receiver replica, Recreate (single writer on the sources claim)"
eq "$(jq -r "$wpod | [.volumes[] | select(.name == \"sources\") | .persistentVolumeClaim.claimName] | join(\",\")" "$work/webhook.json")" \
  "webhook-sources" "the capability store is the sources claim"
eq "$(jq -r "$wpod | [.volumes[] | select(.name == \"tokens\") | .secret] | .[0] | [.secretName, (.optional // false)] | join(\":\")" "$work/webhook.json")" \
  "webhook-tokens:false" "the token volume is fail-closed"
eq "$(jq -r '.[] | select(.kind == "Secret") | .metadata.name' "$work/webhook.json")" "" \
  "the webhook build contains no Secret"
eq "$(jq -r '.[] | select(.kind == "ServiceAccount" and .metadata.name == "webhook") | .automountServiceAccountToken' "$work/webhook.json")" \
  "false" "the webhook ServiceAccount has no token automount"
eq "$(jq -r '.[] | select(.kind == "Service" and .metadata.name == "webhook") | [.spec.ports[0].port, .spec.ports[0].targetPort] | join(":")' "$work/webhook.json")" \
  "8080:http" "the webhook Service exposes 8080 to the http port"
eq "$(jq -r '.[] | select(.kind == "PersistentVolumeClaim" and .metadata.name == "webhook-sources") | [.metadata.namespace, .spec.storageClassName, .spec.accessModes[0]] | join(":")' "$work/webhook.json")" \
  "webhook:local-path:ReadWriteOnce" "the capability store uses the k3s local-path class"

wimage=$(jq -r "$wpod | .containers[0].image" "$work/webhook.json")
case $wimage in
  ghcr.io/smores56/homelabs-platform/webhook@sha256:*) ok "webhook image is the platform repository" ;;
  *) bad "webhook image is not the platform repository ($wimage)" ;;
esac
if [[ $wimage =~ @sha256:[0-9a-f]{64}$ ]]; then
  ok "webhook image is digest-pinned"
else
  bad "webhook image is not digest-pinned ($wimage)"
fi

eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "default-deny") | (.spec.policyTypes // []) | sort | join(",")' "$work/webhook.json")" \
  "Egress,Ingress" "default-deny covers both directions in webhook"
eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "allow-dns-egress") | .spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"]' "$work/webhook.json")" \
  "kube-system" "DNS egress in webhook targets kube-system only"
eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "allow-ingress-cloudflared") | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.ingress[0].from[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.ingress[0].ports[0].port] | join(":")' "$work/webhook.json")" \
  "ingress:cloudflared:8080" "the receiver admits only the tunnel on 8080"
eq "$(jq -r '.[] | select(.kind == "NetworkPolicy" and .metadata.name == "allow-gateway-egress") | [.spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.egress[0].to[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.egress[0].ports[0].port] | join(":")' "$work/webhook.json")" \
  "tenant-apps:nanobot:8765" "the receiver reaches only the gateway's websocket listener"
eq "$(jq -r '[.[] | select(.kind == "NetworkPolicy" and .metadata.namespace == "webhook") | .metadata.name] | sort | join(",")' "$work/webhook.json")" \
  "allow-dns-egress,allow-gateway-egress,allow-ingress-cloudflared,default-deny" \
  "webhook keeps only default-deny, DNS, tunnel ingress and gateway egress"
if jq -r '.[] | select(.kind == "NetworkPolicy") | .spec.egress[]? | .to[]? | .ipBlock // empty' "$work/webhook.json" | grep -q .; then
  bad "the webhook has no public egress"
else
  ok "the webhook has no public egress"
fi
eq "$(jq -r '.[] | select(.kind == "ConfigMap" and .metadata.name == "webhook-tokens-contract") | [.metadata.namespace, .data.secretName, (.data.keys | split(",") | sort | join(","))] | join(":")' "$work/webhook.json")" \
  "webhook:webhook-tokens:admin-token,enqueue-token" \
  "the token contract names the Secret and both keys"

# The remote halves of the two flows live in the network stage.
eq "$(jq -r 'select(.metadata.namespace == "ingress" and .metadata.name == "egress-webhook") | [.spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.egress[0].to[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.egress[0].ports[0].port] | join(":")' "$network_json")" \
  "webhook:webhook:8080" "the tunnel reaches the webhook Service on 8080"
eq "$(jq -r 'select(.metadata.namespace == "tenant-apps" and .metadata.name == "allow-webhook-ingress") | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.ingress[0].from[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.ingress[0].ports[0].port] | join(":")' "$network_json")" \
  "webhook:webhook:8765" "the tenant admits only the receiver on the gateway port"

# ------------------------------------------------ repository mirror (backup)
# The mirror is platform-tier and holds one read-only credential. Its shape is
# asserted here so a widened egress, a classic token or a writable rootfs never
# reaches review.
backup_dir=clusters/homelab/platform/backup
kustomize build "$backup_dir" >"$work/backup.yaml"
json_of "$work/backup.yaml" >"$work/backup.json"

backup_cj='.[] | select(.kind == "CronJob" and .metadata.name == "github-backup")'
eq "$(jq -r "$backup_cj | [.metadata.namespace, .spec.schedule, .spec.timeZone, .spec.concurrencyPolicy] | join(\":\")" "$work/backup.json")" \
  "platform:15 4 * * *:Europe/Berlin:Forbid" \
  "github-backup runs nightly in the platform namespace and never overlaps"
eq "$(jq -r "$backup_cj | .spec.jobTemplate.spec.activeDeadlineSeconds" "$work/backup.json")" \
  "1800" "github-backup is bounded to 30 minutes"

backup_pod="$backup_cj | .spec.jobTemplate.spec.template.spec"
eq "$(jq -r "$backup_pod | .serviceAccountName" "$work/backup.json")" \
  "github-backup" "github-backup runs as its own service account"
eq "$(jq -r "$backup_pod | if has(\"automountServiceAccountToken\") then .automountServiceAccountToken else true end" "$work/backup.json")" \
  "false" "github-backup never mounts a service-account token"
eq "$(jq -r "$backup_pod | .securityContext.runAsNonRoot" "$work/backup.json")" \
  "true" "github-backup runs non-root"
eq "$(jq -r "$backup_pod | .securityContext.runAsUser" "$work/backup.json")" \
  "65532" "github-backup uses the distroless nonroot uid"
eq "$(jq -r "$backup_pod | .containers[0].securityContext.readOnlyRootFilesystem" "$work/backup.json")" \
  "true" "github-backup rootfs is read-only"
eq "$(jq -r "$backup_pod | .containers[0].securityContext.allowPrivilegeEscalation" "$work/backup.json")" \
  "false" "github-backup forbids privilege escalation"
eq "$(jq -r "$backup_pod | .containers[0].securityContext.capabilities.drop | join(\",\")" "$work/backup.json")" \
  "ALL" "github-backup drops every capability"
eq "$(jq -r "$backup_pod | .containers[0].resources.limits | keys | join(\",\")" "$work/backup.json")" \
  "cpu,memory" "github-backup sets resource limits"

eq "$(jq -r "$backup_pod | [.volumes[] | select(.name == \"token\") | .secret] | .[0] | [.secretName, (.optional // false)] | join(\":\")" "$work/backup.json")" \
  "github-backup-read-pat:false" "the PAT volume is fail-closed"
eq "$(jq -r "$backup_pod | [.volumes[] | select(.name == \"backup\") | .persistentVolumeClaim.claimName] | join(\",\")" "$work/backup.json")" \
  "github-backup-data" "the mirror writes to its own PVC"
if jq -e "$backup_pod | .volumes[] | select(.name == \"script\") | .configMap.name | startswith(\"github-backup-script\")" "$work/backup.json" >/dev/null; then
  ok "the backup script is mounted from the generated ConfigMap"
else
  bad "the backup script is mounted from the generated ConfigMap"
fi
if jq -r "$backup_pod | .containers[0].args[0]" "$work/backup.json" | grep -q -- '--repo homelabs-apps'; then
  ok "the job mirrors both repositories"
else
  bad "the job mirrors both repositories"
fi

eq "$(jq -r '.[] | select(.kind == "PersistentVolumeClaim" and .metadata.name == "github-backup-data") | [.metadata.namespace, .spec.storageClassName, .spec.accessModes[0]] | join(":")' "$work/backup.json")" \
  "platform:local-path:ReadWriteOnce" "the mirror claim uses the k3s local-path class"

backup_np='.[] | select(.kind == "NetworkPolicy" and .metadata.name == "github-backup-egress")'
eq "$(jq -r "$backup_np | [.spec.podSelector.matchLabels[\"app.kubernetes.io/name\"], (.spec.policyTypes | join(\",\"))] | join(\":\")" "$work/backup.json")" \
  "github-backup:Egress" "the backup egress policy selects only the backup pod"
eq "$(jq -r "$backup_np | [.spec.egress[] | .ports[]?.port] | unique | sort | join(\",\")" "$work/backup.json")" \
  "53,443" "the backup pod may reach DNS and 443 only"
if jq -r "$backup_np | .spec.egress[] | .to[] | .ipBlock.cidr // empty" "$work/backup.json" | grep -qx '0.0.0.0/0'; then
  bad "backup egress is not a public catch-all"
else
  ok "backup egress is not a public catch-all"
fi
if jq -r "$backup_np | .spec.egress[] | select(.to[0].ipBlock) | .to[0].ipBlock.cidr" "$work/backup.json" | grep -q '^140\.82\.112\.0/20$'; then
  ok "backup egress includes GitHub's git range"
else
  bad "backup egress includes GitHub's git range"
fi

eq "$(jq -r '.[] | select(.kind == "ConfigMap" and .metadata.name == "github-backup-read-pat-contract") | [.metadata.namespace, .data.key, .data.secretName] | join(":")' "$work/backup.json")" \
  "platform:token:github-backup-read-pat" "the PAT contract names the projected Secret and key"
if jq -r '.[] | select(.kind == "ConfigMap" and .metadata.name == "github-backup-read-pat-contract") | .data.scope' "$work/backup.json" | grep -q 'Contents: read'; then
  ok "the PAT contract is read-only"
else
  bad "the PAT contract is read-only"
fi

# A classic token (ghp_) must never appear in a manifest, and the job refuses
# one at start (clusters/homelab/platform/backup/github-backup.sh).
if grep -rEn 'ghp_[A-Za-z0-9]{20,}' clusters/ >/dev/null 2>&1; then
  bad "no classic GitHub token is present"
else
  ok "no classic GitHub token is present"
fi
if grep -rEn 'GITHUB_TOKEN' clusters/homelab/platform/backup clusters/homelab/broker >/dev/null 2>&1; then
  bad "the backup and broker manifests never name GITHUB_TOKEN"
else
  ok "the backup and broker manifests never name GITHUB_TOKEN"
fi
if grep -qiE 'revoke.*classic|classic.*revoke' docs/manual-steps.md; then
  ok "manual steps revoke the classic write PAT"
else
  bad "manual steps revoke the classic write PAT"
fi

# ------------------------------------------------------- stage ordering (DAG)
declare -A stage_edges=(
  [platform-namespaces]=""
  [platform-rbac]="platform-namespaces"
  [platform-policies]="platform-rbac"
  [platform-network]="platform-namespaces"
  [platform-secrets]="platform-namespaces,platform-policies"
  [platform-broker]="platform-namespaces,platform-network,platform-policies,platform-rbac,platform-secrets"
  [platform-agent-control]="platform-namespaces,platform-network,platform-policies,platform-rbac"
  [platform-webhook]="platform-namespaces,platform-network,platform-policies,platform-secrets"
  [platform-storage]="platform-namespaces"
  [platform-backup]="platform-namespaces,platform-policies"
  [platform-digest]="platform-namespaces,platform-observability,platform-policies,platform-storage"
  [platform-data-apps]="platform-network,platform-policies,platform-storage"
  [platform-ingress]="platform-namespaces,platform-network"
  [platform-observability]="platform-policies"
  [platform-nanobot-policy]="platform-namespaces,platform-policies"
  [platform-tenants]="platform-network,platform-policies,platform-secrets"
)

for file in "$stages_dir"/*.yaml; do
  json=$(json_of "$file")
  name=$(jq -r '.[0].metadata.name' <<<"$json")
  sa=$(jq -r '.[0].spec.serviceAccountName // ""' <<<"$json")
  source=$(jq -r '.[0].spec.sourceRef.name // ""' <<<"$json")
  path=$(jq -r '.[0].spec.path // ""' <<<"$json")
  deps=$(jq -r '.[0].spec.dependsOn // [] | map(.name) | sort | join(",")' <<<"$json")
  eq "$sa" "kustomize-controller" "stage $name reconciles as kustomize-controller"
  eq "$source" "flux-system" "stage $name reads the flux-system GitRepository"
  case $path in
    ./clusters/*) ok "stage $name path is inside clusters/ ($path)" ;;
    *) bad "stage $name has an unexpected path ($path)" ;;
  esac
  if [[ -n ${stage_edges[$name]+set} ]]; then
    eq "$deps" "${stage_edges[$name]}" "stage $name dependsOn ${stage_edges[$name]:-nothing}"
  else
    bad "unexpected stage $name"
  fi
done

listed=$(yq -o=json -I=0 '.' clusters/homelab/platform/kustomization.yaml |
  jq -r '[.resources[] | sub("^kustomizations/"; "") | sub("\\.yaml$"; "")] | .[]')
eq "$listed" "$(printf '%s\n' namespaces rbac policies network secrets broker agent-control webhook storage backup digest data ingress observability nanobot-policy tenants)" \
  "platform/kustomization.yaml lists every stage in apply order"
missing=$(comm -23 <(find "$stages_dir" -maxdepth 1 -name '*.yaml' -printf '%f\n' | sed 's|\.yaml$||' | sort) \
  <(yq -o=json -I=0 '.' clusters/homelab/platform/kustomization.yaml |
    jq -r '.resources[] | sub("^kustomizations/"; "") | sub("\\.yaml$"; "")' | sort))
if [[ -z $missing ]]; then
  ok "every stage file is applied by platform/kustomization.yaml"
else
  bad "stage files missing from platform/kustomization.yaml" "$missing"
fi

# ------------------------------------------------------- bootstrap and sync
if diff -u <(clusters/homelab/flux-system/bootstrap.sh --print-sync) \
  clusters/homelab/flux-system/gotk-sync.yaml >"$work/sync.diff"; then
  ok "gotk-sync.yaml matches bootstrap.sh --print-sync"
else
  bad "gotk-sync.yaml drifted from bootstrap.sh --print-sync" "$(cat "$work/sync.diff")"
fi

# Flag and value are asserted separately: bootstrap.sh passes shell variables, so
# a single literal grep would test the wrong string.
while IFS=$'\t' read -r flag value; do
  [[ -z $flag || $flag == \#* ]] && continue
  found=1
  grep -Fq -- "${flag}" clusters/homelab/flux-system/bootstrap.sh || found=0
  if [[ -n $value ]]; then
    grep -Fq -- "$value" clusters/homelab/flux-system/bootstrap.sh || found=0
  fi
  if ((found)); then
    ok "bootstrap.sh passes ${flag} ${value}"
  else
    bad "bootstrap.sh is missing ${flag} ${value}"
  fi
done <<'FLAGS'
--owner	smores56
--repository	homelabs-platform
--branch	main
--path	clusters/homelab
--components-extra	source-watcher
--components-extra	image-reflector-controller
--components-extra	image-automation-controller
--personal
FLAGS

sync_json=$(json_of clusters/homelab/flux-system/gotk-sync.yaml)
eq "$(jq -r '[.[] | select(.kind == "Kustomization")][0].spec.serviceAccountName' <<<"$sync_json")" \
  "kustomize-controller" "root Kustomization is pinned to kustomize-controller"
eq "$(jq -r '[.[] | select(.kind == "Kustomization")][0].spec.path' <<<"$sync_json")" \
  "./clusters/homelab" "root Kustomization path is the cluster directory"

tenant_json=$(json_of clusters/homelab/platform/tenants/homelabs-apps.yaml)
eq "$(jq -r '[.[] | select(.kind == "GitRepository")][0].spec.url' <<<"$tenant_json")" \
  "ssh://git@ssh.github.com:443/smores56/homelabs-apps" \
  "tenant source uses GitHub SSH on 443 (the tap forwards only 80/443)"
eq "$(jq -r '[.[] | select(.kind == "Kustomization")][0].spec.serviceAccountName' <<<"$tenant_json")" \
  "tenant-apps-reconciler" "tenant Kustomization uses the tenant service account"
eq "$(jq -r '[.[] | select(.kind == "Kustomization")][0].spec.decryption.secretRef.name' <<<"$tenant_json")" \
  "sops-age" "tenant Kustomization decrypts with sops-age"
eq "$(jq -r '[.[] | select(.kind == "Kustomization")][0].spec.targetNamespace' <<<"$tenant_json")" \
  "tenant-apps" "tenant Kustomization targets its own namespace"
if grep -Fq "tenant-apps-reconciler" "$policies_dir/06-tenant-flux-guard.yaml" &&
  grep -Fq "homelabs-apps" "$policies_dir/06-tenant-flux-guard.yaml"; then
  ok "tenant-flux-guard pins the same service account and source"
else
  bad "tenant-flux-guard does not pin the reconciled values"
fi

# --------------------------------------------------------- fixture/table sync
mapfile -t fixture_files < <(find "$fixtures_dir" -maxdepth 1 -name '*.yaml' -printf '%f\n' | sort)
mapfile -t table_files < <(grep -v '^#' "$fixtures_dir/expectations.tsv" | cut -f1 | sort)
eq "${table_files[*]}" "${fixture_files[*]}" "expectations.tsv covers every fixture exactly once"

while IFS=$'\t' read -r fixture _ns expect _policy _token; do
  [[ -z $fixture || $fixture == \#* ]] && continue
  header=$(head -1 "$fixtures_dir/$fixture")
  expected_header="# expect: ${expect}"
  [[ $expect == allow ]] || expected_header="# expect: deny ${_policy}"
  eq "$header" "$expected_header" "fixture header matches table ($fixture)"
done <"$fixtures_dir/expectations.tsv"

# ---------------------------------------------------------------------- runtime
if ((runtime)); then
  command -v kubectl >/dev/null 2>&1 || {
    echo "runtime mode needs kubectl" >&2
    exit 1
  }
  kubeconfig="$external_kubeconfig"
  if [[ -z $kubeconfig ]]; then
    kubeconfig=$(tests/policies/apiserver.sh start) || exit 1
    apiserver_started=1
  fi
  export KUBECONFIG="$kubeconfig"

  # Flux CRDs, so Kustomization fixtures are admitted against a real schema.
  if kubectl apply -f clusters/homelab/flux-system/gotk-components.yaml >/dev/null 2>"$work/crds.err"; then
    ok "runtime: flux components applied to the throwaway cluster"
  else
    bad "runtime: applying flux components failed" "$(cat "$work/crds.err")"
  fi
  for stage in namespaces rbac policies network secrets storage backup policy tenants; do
    if kubectl apply -k "clusters/homelab/platform/$stage" >/dev/null 2>"$work/stage.err"; then
      ok "runtime: stage $stage applied"
    else
      bad "runtime: stage $stage failed to apply" "$(cat "$work/stage.err")"
    fi
  done

  # The status exporter's identity lives in the observability stage, which needs
  # the ESO CRDs. Its RBAC is four plain objects, so apply exactly those: the
  # workload ClusterRole, the nodes-only ClusterRole, the ClusterRoleBinding that
  # makes `nodes` reachable, and the per-data-namespace RoleBindings.
  exporter_rbac=0
  while IFS= read -r object; do
    if kubectl apply -f "clusters/homelab/observability/$object" >/dev/null 2>"$work/exporter.err"; then
      exporter_rbac=$((exporter_rbac + 1))
    else
      bad "runtime: apply observability/$object" "$(cat "$work/exporter.err")"
    fi
  done <<'EXPORTER_RBAC'
status-exporter/clusterrole.yaml
status-exporter/clusterrole-nodes.yaml
status-exporter/clusterrolebinding.yaml
rbac-data.yaml
EXPORTER_RBAC
  eq "$exporter_rbac" "4" "runtime: status-exporter RBAC applied (both ClusterRoles, the ClusterRoleBinding, the RoleBindings)"

  # The agent-control identity's namespaced half is in the RBAC stage above; its
  # cluster-scoped half lives with the workload, so apply exactly those two
  # objects before the can-i rows for subject `control`.
  control_rbac=0
  while IFS= read -r object; do
    if kubectl apply -f "clusters/homelab/agent-control/$object" >/dev/null 2>"$work/control.err"; then
      control_rbac=$((control_rbac + 1))
    else
      bad "runtime: apply agent-control/$object" "$(cat "$work/control.err")"
    fi
  done <<'CONTROL_RBAC'
clusterrole.yaml
clusterrolebinding.yaml
CONTROL_RBAC
  eq "$control_rbac" "2" "runtime: agent-control ClusterRole and ClusterRoleBinding applied"

  # PSA would otherwise answer first for host-access fixtures, which would make
  # the assertion pass for the wrong reason. The shipped labels stay restricted;
  # this throwaway cluster relaxes them so each denial is attributable to the
  # policy named in the table (docs/negative-tests.md).
  while IFS= read -r ns; do
    kubectl label namespace "$ns" pod-security.kubernetes.io/enforce=privileged --overwrite >/dev/null 2>&1 || true
  done < <(kubectl get namespaces -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

  while IFS=$'\t' read -r fixture ns expect policy _token; do
    [[ -z $fixture || $fixture == \#* ]] && continue
    out=$(kubectl apply --dry-run=server -n "$ns" -f "$fixtures_dir/$fixture" 2>&1)
    status=$?
    if [[ $expect == allow ]]; then
      if ((status == 0)); then ok "runtime: admit $fixture"; else bad "runtime: admit $fixture" "$out"; fi
    elif ((status == 0)); then
      bad "runtime: deny $fixture by $policy" "request was admitted"
    elif [[ $out == *"ValidatingAdmissionPolicy '$policy'"* ]]; then
      ok "runtime: deny $fixture by $policy"
    else
      bad "runtime: deny $fixture by $policy" "$out"
    fi
  done <"$fixtures_dir/expectations.tsv"

  tenant_sa="system:serviceaccount:tenant-apps:tenant-apps-reconciler"
  agent_sa="system:serviceaccount:tenant-apps:nanobot"
  exporter_sa="system:serviceaccount:observability:status-exporter"
  control_sa="system:serviceaccount:tenant-apps:agent-control"
  while IFS=$'\t' read -r verb resource ns expect subject; do
    [[ -z $verb || $verb == \#* ]] && continue
    case ${subject:-tenant} in
      tenant) sa=$tenant_sa ;;
      agent) sa=$agent_sa ;;
      exporter) sa=$exporter_sa ;;
      control) sa=$control_sa ;;
      *)
        bad "unknown rbac subject ${subject:-}"
        continue
        ;;
    esac
    if [[ $ns == "-" ]]; then
      got=$(kubectl auth can-i "$verb" "$resource" --as "$sa" 2>/dev/null)
    else
      got=$(kubectl auth can-i "$verb" "$resource" -n "$ns" --as "$sa" 2>/dev/null)
    fi
    eq "${got:-error}" "$expect" "runtime: rbac ${subject:-tenant} $verb $resource ($ns)"
  done <tests/policies/rbac.tsv

  out=$(kubectl create secret generic probe --from-literal=k=v --dry-run=server \
    -n tenant-apps --as "$tenant_sa" 2>&1)
  if [[ $out == *forbidden* && $out != *"created (server dry run)"* ]]; then
    ok "runtime: tenant cannot create a Secret over the API"
  else
    bad "runtime: tenant created a Secret" "$out"
  fi

  # The prompt and model catalogue are platform-owned: the tenant reconciler has
  # configmaps write, so only this policy stops a tenant push from rewriting them.
  out=$(kubectl apply --dry-run=server -n tenant-apps --as "$tenant_sa" -f - 2>&1 <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: nanobot-policy
  namespace: tenant-apps
data:
  system.md: "tenant override"
YAML
)
  if [[ $out == *"ValidatingAdmissionPolicy 'nanobot-policy-write'"* ]]; then
    ok "runtime: tenant cannot rewrite the platform nanobot-policy ConfigMap"
  else
    bad "runtime: tenant rewrote nanobot-policy" "$out"
  fi

  out=$(kubectl apply --dry-run=server -n tenant-apps --as "$tenant_sa" \
    -f "$fixtures_dir/tenant-kustomization-good.yaml" 2>&1)
  if [[ $out == *forbidden* && $out != *"created (server dry run)"* ]]; then
    ok "runtime: tenant cannot create a Kustomization"
  else
    bad "runtime: tenant created a Kustomization" "$out"
  fi
fi

# --------------------------------------------------------- CI wiring (static)
# The runtime half is only worth having if CI runs it: a vacuous CEL expression
# passes the static half by construction. The job name is the branch-protection
# context, so it is asserted here and in docs/runbook.md.
if grep -Fq 'tests/policies/check.sh --runtime' .github/workflows/ci.yml; then
  ok "ci.yml runs the runtime half"
else
  bad "ci.yml does not run tests/policies/check.sh --runtime"
fi
if grep -Fq 'name: policy admission runtime' .github/workflows/ci.yml &&
  grep -Fq 'policy admission runtime' docs/runbook.md; then
  ok "the policy admission runtime job is a documented required context"
else
  bad "the policy admission runtime job is missing or undocumented"
fi
if grep -Fq 'tests/policies/apiserver.sh stop' .github/workflows/ci.yml; then
  ok "ci.yml always stops the throwaway control plane"
else
  bad "ci.yml leaves the throwaway control plane running"
fi

# ---------------------------------------------------------------------- summary
mode="static"
((runtime)) && mode="static+runtime"
printf '\n%s: %d passed, %d failed\n' "$mode" "$pass" "$fail"
((fail == 0)) || exit 1

# vim: ft=bash
