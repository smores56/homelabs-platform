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
  "calibre:data-workload,graphiti:data-workload,immich:data-workload,navidrome:data-workload,tenant-apps:tenant-apps-reconciler" \
  "the RBAC stage holds the tenant identity and one data-workload account per data namespace"
eq "$(jq -r 'select(.kind == "Role" or .kind == "RoleBinding") | .kind' "$rbac_json" | sort | paste -sd, -)" \
  "Role,RoleBinding" "the RBAC stage holds no other Role or RoleBinding"

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

eq "$(jq -r 'select(.kind == "Role") | .metadata.namespace' "$rbac_json")" "tenant-apps" "tenant Role is namespace-scoped"
eq "$(jq -r 'select(.kind == "RoleBinding") | .subjects | map("\(.kind):\(.namespace):\(.name)") | join(",")' "$rbac_json")" \
  "ServiceAccount:tenant-apps:tenant-apps-reconciler" "rolebinding binds only the tenant service account"
eq "$(jq -r 'select(.kind == "Role") | [.rules[] | select(.resources | index("pods")) | .verbs | index("create")] | map(select(. != null)) | length' "$rbac_json")" \
  "1" "tenant Role can create pods (the tenant can still work)"

# ------------------------------------------------------------ controller flags
declare -A controller_flags=(
  [kustomize-controller]="--no-cross-namespace-refs=true --no-remote-bases=true --default-service-account=default --default-decryption-service-account=default --default-kubeconfig-service-account=default"
  [helm-controller]="--no-cross-namespace-refs=true --default-service-account=default --default-kubeconfig-service-account=default"
  [notification-controller]="--no-cross-namespace-refs=true --default-service-account=default"
  [source-controller]="--default-service-account=default"
  [source-watcher]="--no-cross-namespace-refs=true"
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
while IFS=: read -r app port; do
  eq "$(jq -r --arg ns "$app" --arg name "$app-http-from-ingress" \
    'select(.metadata.namespace == $ns and .metadata.name == $name) | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], .spec.ingress[0].from[0].podSelector.matchLabels["app.kubernetes.io/name"], .spec.ingress[0].ports[0].port] | join(":")' "$network_json")" \
    "ingress::$port" "$app accepts HTTP from the ingress namespace only"
done <<'APPS'
immich:2283
navidrome:4533
calibre:8083
graphiti:8184
APPS

# Exact policy sets: an added flow has to be added to this assertion too, which
# is what keeps "only the intended flows" reviewable.
eq "$(jq -s -r 'map(select(.metadata.namespace == "immich") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-same-namespace,default-deny,immich-http-from-ingress,immich-ml-public-https" \
  "immich keeps only default-deny, DNS, same-namespace, ingress HTTP and model-download HTTPS"
eq "$(jq -s -r 'map(select(.metadata.namespace == "navidrome") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-same-namespace,default-deny,navidrome-http-from-ingress" \
  "navidrome keeps only default-deny, DNS, same-namespace and ingress HTTP"
eq "$(jq -s -r 'map(select(.metadata.namespace == "calibre") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-same-namespace,calibre-http-from-ingress,default-deny" \
  "calibre keeps only default-deny, DNS, same-namespace and ingress HTTP"
eq "$(jq -s -r 'map(select(.metadata.namespace == "graphiti") | .metadata.name) | sort | join(",")' "$network_json")" \
  "allow-dns-egress,allow-same-namespace,default-deny,graphiti-http-from-ingress,graphiti-public-https" \
  "graphiti keeps only default-deny, DNS, same-namespace, ingress HTTP and Neuralwatt HTTPS"

eq "$(jq -r 'select(.metadata.namespace == "ingress" and .metadata.name == "egress-data-apps-only") | [.spec.egress[] | .to[0] | "\(.namespaceSelector.matchLabels["kubernetes.io/metadata.name"]):\(.podSelector.matchLabels["app.kubernetes.io/name"])"] | sort | join(",")' "$network_json")" \
  "calibre:calibre,graphiti:graphiti,immich:immich,navidrome:navidrome" \
  "the ingress namespace reaches the four app Services and nothing else"
eq "$(jq -r 'select(.metadata.namespace == "ingress" and .metadata.name == "egress-tunnel-edge") | [.spec.podSelector.matchLabels["app.kubernetes.io/name"], .spec.egress[0].ports[0].port] | join(":")' "$network_json")" \
  "cloudflared:443" "cloudflared may reach the Cloudflare edge over TCP 443 only"

# ------------------------------------------------------- data storage contract
storage_dir=clusters/homelab/platform/storage
sc="$storage_dir/storageclass.yaml"
pvs="$storage_dir/persistentvolumes.yaml"

eq "$(yq -r '.provisioner' "$sc")" "kubernetes.io/no-provisioner" "data-local has no provisioner"
eq "$(yq -r '.reclaimPolicy' "$sc")" "Retain" "data-local retains volumes"
eq "$(yq -r '.volumeBindingMode' "$sc")" "WaitForFirstConsumer" "data-local binds in the consuming pod's node"
eq "$(yq -r '.allowVolumeExpansion' "$sc")" "false" "data-local is not expandable"

eq "$(yq -r '.spec.local.path' "$pvs" | grep -v '^---$' | sort | paste -sd, -)" \
  "/mnt/data/books,/mnt/data/graphiti,/mnt/data/immich,/mnt/data/music" \
  "every PV path is one of the guest's virtiofs shares"
eq "$(yq -r '.spec.storageClassName' "$pvs" | grep -v '^---$' | sort -u)" "data-local" "every PV claims data-local"
eq "$(yq -r '.spec.persistentVolumeReclaimPolicy' "$pvs" | grep -v '^---$' | sort -u)" "Retain" "every PV retains its data"
eq "$(yq -r '.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].key' "$pvs" | grep -v '^---$' | sort -u)" \
  "kubernetes.io/hostname" "every PV pins node affinity"
eq "$(yq -r '.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]' "$pvs" | grep -v '^---$' | sort -u)" \
  "cluster" "every PV is bound to the cluster node"
eq "$(yq -r '.spec.claimRef | [.namespace, .name] | join(":")' "$pvs" | grep -v '^---$' | sort | paste -sd, -)" \
  "calibre:calibre-data,graphiti:graphiti-data,immich:immich-data,navidrome:navidrome-data" \
  "each PV is pre-bound to exactly one app claim"
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
eq "$(jq -r "$jq_workloads | select(podspec.securityContext.runAsUser != 1000) | .metadata.name" "$work/data-apps.json")" \
  "" "every data pod runs as uid 1000 (the share's host owner through the guest translation)"
eq "$(jq -r "$jq_workloads | podspec.containers[] | select((.resources.limits // {}) == {}) | .name" "$work/data-apps.json")" \
  "" "every data container sets resource limits"
# Probes are for long-running containers; a dump job's own exit code is its
# liveness signal.
eq "$(jq -r '.[] | select(.kind == "Deployment" or .kind == "StatefulSet") | .spec.template.spec.containers[] | select((.readinessProbe // .livenessProbe // .startupProbe) == null) | .name' "$work/data-apps.json")" \
  "" "every long-running data container has at least one probe"

# --------------------------------------------------------- hostname contract
while IFS=: read -r ns host svc port; do
  file="clusters/homelab/data/$ns/ingress.yaml"
  eq "$(yq -r '.spec.rules[0].host' "$file")" "$host" "$host is declared in $ns"
  eq "$(yq -r '.spec.rules[0].http.paths[0].backend.service.name' "$file")" "$svc" \
    "$host routes to the $svc Service"
  eq "$(yq -r '.spec.rules[0].http.paths[0].backend.service.port.number' "$file")" "$port" \
    "$host routes to port $port"
done <<'INGRESS'
immich:immich.sammohr.dev:immich-server:2283
navidrome:music.sammohr.dev:navidrome:4533
calibre:calibre.sammohr.dev:calibre:8083
graphiti:memory.sammohr.dev:graphiti:8184
INGRESS

# ------------------------------------------------------- stage ordering (DAG)
declare -A stage_edges=(
  [platform-namespaces]=""
  [platform-rbac]="platform-namespaces"
  [platform-policies]="platform-rbac"
  [platform-network]="platform-namespaces"
  [platform-secrets]="platform-namespaces,platform-policies"
  [platform-storage]="platform-namespaces"
  [platform-data-apps]="platform-network,platform-policies,platform-storage"
  [platform-ingress]="platform-namespaces,platform-network"
  [platform-observability]="platform-policies"
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
eq "$listed" "$(printf '%s\n' namespaces rbac policies network secrets storage data ingress observability tenants)" \
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
--personal
FLAGS

sync_json=$(json_of clusters/homelab/flux-system/gotk-sync.yaml)
eq "$(jq -r '[.[] | select(.kind == "Kustomization")][0].spec.serviceAccountName' <<<"$sync_json")" \
  "kustomize-controller" "root Kustomization is pinned to kustomize-controller"
eq "$(jq -r '[.[] | select(.kind == "Kustomization")][0].spec.path' <<<"$sync_json")" \
  "./clusters/homelab" "root Kustomization path is the cluster directory"

tenant_json=$(json_of clusters/homelab/platform/tenants/homelabs-apps.yaml)
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
  for stage in namespaces rbac policies network secrets storage tenants; do
    if kubectl apply -k "clusters/homelab/platform/$stage" >/dev/null 2>"$work/stage.err"; then
      ok "runtime: stage $stage applied"
    else
      bad "runtime: stage $stage failed to apply" "$(cat "$work/stage.err")"
    fi
  done

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
  while IFS=$'\t' read -r verb resource ns expect; do
    [[ -z $verb || $verb == \#* ]] && continue
    if [[ $ns == "-" ]]; then
      got=$(kubectl auth can-i "$verb" "$resource" --as "$tenant_sa" 2>/dev/null)
    else
      got=$(kubectl auth can-i "$verb" "$resource" -n "$ns" --as "$tenant_sa" 2>/dev/null)
    fi
    eq "${got:-error}" "$expect" "runtime: rbac $verb $resource ($ns)"
  done <tests/policies/rbac.tsv

  out=$(kubectl create secret generic probe --from-literal=k=v --dry-run=server \
    -n tenant-apps --as "$tenant_sa" 2>&1)
  if [[ $out == *forbidden* && $out != *"created (server dry run)"* ]]; then
    ok "runtime: tenant cannot create a Secret over the API"
  else
    bad "runtime: tenant created a Secret" "$out"
  fi

  out=$(kubectl apply --dry-run=server -n tenant-apps --as "$tenant_sa" \
    -f "$fixtures_dir/tenant-kustomization-good.yaml" 2>&1)
  if [[ $out == *forbidden* && $out != *"created (server dry run)"* ]]; then
    ok "runtime: tenant cannot create a Kustomization"
  else
    bad "runtime: tenant created a Kustomization" "$out"
  fi
fi

# ---------------------------------------------------------------------- summary
mode="static"
((runtime)) && mode="static+runtime"
printf '\n%s: %d passed, %d failed\n' "$mode" "$pass" "$fail"
((fail == 0)) || exit 1

# vim: ft=bash
