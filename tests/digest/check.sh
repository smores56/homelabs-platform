#!/usr/bin/env bash
# Cluster digest checks. Static only: no cluster, no credentials.
#
#   tests/digest/check.sh
#
# Proves the digest builder's redaction, determinism and size bounds with
# fixtures, then renders the digest kustomization and asserts its contract:
# 15-minute schedule, one ESO token and nothing else, a static PV/PVC pair, an
# egress allowlist of exactly two sources plus DNS, no ingress into the job, and
# no Service or reverse path that could carry a reply to Kanbei.
#
# kustomize build of every tree and kubeconform are separate CI jobs; they pick
# up clusters/ automatically. yamllint and shellcheck cover this file and the
# manifests in their own jobs.
set -uo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root" || exit 1

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

for tool in yq jq kustomize python3 git; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "missing required tool: $tool" >&2
    echo "nix shell nixpkgs#yq-go nixpkgs#jq nixpkgs#kustomize nixpkgs#python3 -c tests/digest/check.sh" >&2
    exit 1
  }
done

export PYTHONDONTWRITEBYTECODE=1
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

digest_dir=clusters/homelab/platform/digest
builder="$digest_dir/digest_builder.py"
fixtures=tests/digest/fixtures

# ------------------------------------------------------------- builder unit tests
if python3 -m unittest discover -s tests/digest >"$work/unit.log" 2>&1; then
  ok "digest unit tests: $(grep -oE 'Ran [0-9]+ tests' "$work/unit.log" | head -1)"
else
  bad "digest unit tests failed" "$(tail -30 "$work/unit.log")"
fi

# ------------------------------------------------------------------ render
if ! kustomize build "$digest_dir" >"$work/digest.yaml" 2>"$work/digest.err"; then
  echo "kustomize build $digest_dir failed:" >&2
  cat "$work/digest.err" >&2
  exit 1
fi
yq -o=json -I=0 '.' "$work/digest.yaml" | jq -s '.' >"$work/digest.json"
kustomize build clusters/homelab/observability >"$work/observability.yaml" 2>/dev/null
yq -o=json -I=0 '.' "$work/observability.yaml" | jq -s '.' >"$work/observability.json"
renders=("$work/observability.json" "$work/digest.json")

# ------------------------------------------------------ builder vs shipped copy
# The ConfigMap is generated from this file; a stale embedded copy would run
# code no test has seen.
embedded="$work/embedded.py"
jq -j '.[] | select(.kind == "ConfigMap") | .data["digest_builder.py"]' "$work/digest.json" >"$embedded"
if diff -q "$builder" "$embedded" >/dev/null; then
  ok "the shipped ConfigMap embeds the tested digest_builder.py byte for byte"
else
  bad "the ConfigMap copy and $builder differ"
fi

# ------------------------------------------------------------- failures closed
eq "$(jq -r '[.[] | select(.kind == "Namespace") | .metadata.name] | join(",")' "$work/digest.json")" "" \
  "the digest build creates no namespace (it targets observability)"
eq "$(jq -r '[.[] | .metadata.namespace] | unique | join(",")' "$work/digest.json")" "observability" \
  "every digest object is in observability (the token Secret's namespace)"
eq "$(jq -r '.[] | select(.kind == "CronJob") | .metadata.labels["homelabs.dev/tier"]' "$work/digest.json")" \
  "platform" "the CronJob is platform-tier"
eq "$(jq -r '.[] | select(.kind == "ServiceAccount") | [.metadata.name, .automountServiceAccountToken] | join(":")' "$work/digest.json")" \
  "cluster-digest:false" "the digest ServiceAccount never mounts a token"

cj='.[] | select(.kind == "CronJob") | .'
podspec="${cj}spec.jobTemplate.spec.template.spec"
eq "$(jq -r "${cj}spec.schedule" "$work/digest.json")" "*/15 * * * *" \
  "the digest runs every 15 minutes"
eq "$(jq -r "${cj}spec.timeZone" "$work/digest.json")" "UTC" \
  "the schedule is UTC so generated_at is monotonic across DST"
eq "$(jq -r "${cj}spec.concurrencyPolicy" "$work/digest.json")" "Forbid" \
  "the digest never overlaps itself"
eq "$(jq -r "${cj}spec.startingDeadlineSeconds" "$work/digest.json")" "600" \
  "a missed start is retried inside 10 minutes"
eq "$(jq -r "${cj}spec.jobTemplate.spec.activeDeadlineSeconds" "$work/digest.json")" "300" \
  "a run is bounded to 5 minutes"
eq "$(jq -r "${cj}spec.jobTemplate.spec.backoffLimit" "$work/digest.json")" "1" \
  "a failed run retries once"
eq "$(jq -r "${podspec}.automountServiceAccountToken" "$work/digest.json")" "false" \
  "the pod has no service-account token"
eq "$(jq -r "${podspec}.securityContext | [.runAsNonRoot, .runAsUser, .runAsGroup, .seccompProfile.type] | map(tostring) | join(\":\")" "$work/digest.json")" \
  "true:1000:0:RuntimeDefault" \
  "the pod runs as the uid the data shares are owned by, with the default seccomp profile"
eq "$(jq -r "${podspec}.containers[0].securityContext | [.allowPrivilegeEscalation, .readOnlyRootFilesystem, (.capabilities.drop | join(\",\"))] | map(tostring) | join(\":\")" "$work/digest.json")" \
  "false:true:ALL" "the container is unprivileged with a read-only root filesystem"
eq "$(jq -r "${podspec}.containers[0].image" "$work/digest.json")" "python:3.13.12-slim" \
  "the builder runs on the pinned slim image"
eq "$(jq -r "${podspec}.containers[0].resources.limits.memory" "$work/digest.json")" "128Mi" \
  "the container has a memory limit"

while IFS=: read -r name want; do
  got=$(jq -r --arg name "$name" "${podspec}.containers[0].env[] | select(.name == \$name) | .value" "$work/digest.json")
  eq "$got" "$want" "the CronJob sets $name=$want"
done <<'ENV'
DIGEST_DIR:/mnt/data/cluster-digest
DIGEST_MAX_BYTES:65536
DIGEST_MAX_EVENTS:400
DIGEST_MAX_FILES:96
DIGEST_WINDOW:15m
STATUS_EXPORTER_URL:http://status-exporter.observability.svc.cluster.local:9101/metrics
LOKI_GATEWAY_URL:http://loki-gateway.observability.svc.cluster.local:3100
ENV

# --------------------------------------------------------------- secret scope
eq "$(jq -r '[.[] | .. | objects | select(has("secretName")) | .secretName] | unique | join(",")' "$work/digest.json")" \
  "loki-gateway-token" "the digest mounts exactly one Secret"
eq "$(jq -r '[.[] | .. | objects | select(has("secretKeyRef"))] | length' "$work/digest.json")" "0" \
  "no secret reaches the container through env"
eq "$(jq -r "${podspec}.volumes[] | select(.name == \"token\") | [.secret.secretName, (.secret.optional // false), .secret.items[0].key, .secret.items[0].path] | map(tostring) | join(\":\")" "$work/digest.json")" \
  "loki-gateway-token:false:password:token" \
  "the token volume is fail-closed and projects the ESO password as the token file"
eq "$(jq -r "${podspec}.containers[0].env[] | select(.name == \"LOKI_GATEWAY_TOKEN_FILE\") | .value" "$work/digest.json")" \
  "/var/run/secrets/loki-gateway/token" "the builder reads the projected token file"
eq "$(jq -r "${podspec}.volumes[] | select(.name == \"digest\") | .persistentVolumeClaim.claimName" "$work/digest.json")" \
  "cluster-digest-data" "the digest writes the cluster-digest claim"

# ------------------------------------------------------------------ storage
eq "$(jq -r '.[] | select(.kind == "PersistentVolumeClaim") | [.metadata.name, .spec.storageClassName, (.spec.accessModes | join(","))] | join(":")' "$work/digest.json")" \
  "cluster-digest-data:data-local:ReadWriteOnce" \
  "the claim is the static data-local volume, one writer"
pvs=clusters/homelab/platform/storage/persistentvolumes.yaml
yq -o=json '.' "$pvs" | jq -s '.' >"$work/pvs.json"
pv='.[] | select(.metadata.name == "cluster-digest-share")'
eq "$(jq -r "$pv | .spec.local.path" "$work/pvs.json")" \
  "/mnt/data/cluster-digest" "the PV points at the guest's digest share"
eq "$(jq -r "$pv | .spec.claimRef | [.namespace, .name] | join(\":\")" "$work/pvs.json")" \
  "observability:cluster-digest-data" "the PV is pre-bound to the one claim"
eq "$(jq -r "$pv | [.spec.storageClassName, .spec.persistentVolumeReclaimPolicy, .spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]] | join(\":\")" "$work/pvs.json")" \
  "data-local:Retain:cluster" "the PV retains data and pins node affinity to cluster"

# ------------------------------------------------------- egress allowlist
egress_policy='.[] | select(.kind == "NetworkPolicy" and .metadata.name == "cluster-digest-egress")'
eq "$(jq -r "$egress_policy | (.spec.policyTypes | sort | join(\",\"))" "$work/digest.json")" \
  "Egress,Ingress" "the digest pod declares both policy directions"
eq "$(jq -r "$egress_policy | (.spec.ingress // []) | length" "$work/digest.json")" "0" \
  "the digest pod accepts no ingress (no reverse channel)"

while IFS=$'\t' read -r kind rest; do
  [[ -z $kind || $kind == \#* ]] && continue
  [[ $kind == allow ]] || continue
  ns="" pods="" port=""
  IFS=$'\t' read -ra parts <<<"$rest"
  for part in "${parts[@]}"; do
    case $part in
      namespace=*) ns=${part#*=} ;;
      pod=*) pods=${part#*=} ;;
      port=*) port=${part#*=} ;;
    esac
  done
  target="$ns"
  [[ $pods != "*" ]] && target="$ns/$pods"
  got=$(jq -r --arg ns "$ns" --arg pod "$pods" --argjson port "$port" \
    "$egress_policy"' | [.spec.egress[] | select(.to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"] == $ns) | select(($pod == "*") or (.to[0].podSelector.matchLabels["app.kubernetes.io/name"] == $pod)) | select(.ports | any(.port == $port))] | length' \
    "$work/digest.json")
  eq "$got" "1" "egress allows $target on $port"
done <"$fixtures/egress.tsv"

eq "$(jq -r "$egress_policy | [.spec.egress[] | select((.to // []) == [])] | length" "$work/digest.json")" \
  "0" "no egress rule omits its destination (an empty to allows everything)"
eq "$(jq -r "$egress_policy | [.spec.egress[].ports[].port] | unique | sort | map(tostring) | join(\",\")" "$work/digest.json")" \
  "53,3100,9101" "the digest may open only DNS, the exporter and the gateway ports"
eq "$(jq -r "$egress_policy | [.spec.egress[].to[] | .ipBlock.cidr // empty] | join(\",\")" "$work/digest.json")" \
  "" "the digest never uses an ipBlock (no public egress to Cloudflare, GitHub or Neuralwatt)"

while IFS=$'\t' read -r kind value; do
  [[ -z $kind || $kind == \#* ]] && continue
  [[ $kind == deny ]] || continue
  key=${value%%=*}
  needle=${value#*=}
  case $key in
    namespace)
      got=$(jq -r --arg v "$needle" "$egress_policy"' | [.spec.egress[] | .to[] | select(.namespaceSelector.matchLabels["kubernetes.io/metadata.name"] == $v)] | length' "$work/digest.json")
      ;;
    cidr)
      got=$(jq -r --arg v "$needle" "$egress_policy"' | [.spec.egress[] | .to[] | select(.ipBlock.cidr == $v)] | length' "$work/digest.json")
      ;;
    port)
      got=$(jq -r --argjson v "$needle" "$egress_policy"' | [.spec.egress[].ports[] | select(.port == $v)] | length' "$work/digest.json")
      ;;
    *)
      bad "unknown deny fixture: $value"
      continue
      ;;
  esac
  eq "$got" "0" "egress never reaches $value"
done <"$fixtures/egress.tsv"

# ------------------------------------------------------- no reverse path/Service
if jq -e '.[] | select(.kind == "Service")' "$work/digest.json" >/dev/null; then
  bad "the digest build declares a Service"
else
  ok "the digest build declares no Service"
fi

hits=$(jq -s -r 'add | .[] | select(.kind == "Service") | select(.spec.selector["app.kubernetes.io/name"] == "cluster-digest") | .metadata.name' \
  "${renders[@]}")
if [[ -z $hits ]]; then ok "no Service selects the digest pod"; else bad "a Service selects the digest pod" "$hits"; fi

while IFS= read -r ns; do
  [[ -z $ns || $ns == \#* ]] && continue
  hits=$(jq -s -r --arg ns "$ns" '
    add | [.[] | select(.kind == "NetworkPolicy" and .metadata.namespace == $ns)
    | (.spec.ingress // [])[] | (.from // [])[]
    | select(.podSelector.matchLabels["app.kubernetes.io/name"] == "cluster-digest")]
    | length' "${renders[@]}")
  eq "$hits" "0" "no ingress in $ns comes from the digest pod"
done <"$fixtures/reverse-path.txt"

hits=$(jq -s -r '
  add | .[] | select(.kind == "NetworkPolicy") | . as $policy
  | ($policy.spec.ingress // [])[] | (.from // [])[]
  | select(.podSelector.matchLabels["app.kubernetes.io/name"] == "cluster-digest")
  | $policy.metadata.name' "${renders[@]}" | grep -v -E '^(status-exporter|loki-gateway)-from-digest$' || true)
if [[ -z $hits ]]; then
  ok "the digest is a source for exactly the exporter and gateway ingress policies"
else
  bad "a NetworkPolicy opens the digest as a source somewhere else" "$hits"
fi

hits=$(grep -rl 'cluster-digest' clusters/homelab/platform/rbac \
  clusters/homelab/observability/rbac-data.yaml \
  clusters/homelab/observability/status-exporter 2>/dev/null || true)
if [[ -z $hits ]]; then ok "no RBAC object or source names the digest identity"; else bad "RBAC names the digest identity" "$hits"; fi

# --------------------------------------------------- determinism and size bound
if python3 - "$builder" "$fixtures" <<'PY' >"$work/determinism.log" 2>&1; then
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

builder, fixtures = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("digest_builder", builder)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
payload = json.loads((fixtures / "loki-planted.json").read_text())
metrics = (fixtures / "status-metrics.txt").read_text()
moment = datetime(2026, 11, 11, 4, 10, 0, tzinfo=timezone.utc)
first = module.encode(module.build(exporter_text=metrics, loki_payload=payload, generated_at=moment))
second = module.encode(module.build(exporter_text=metrics, loki_payload=payload, generated_at=moment))
assert first == second, "digest is not deterministic"
assert len(first) <= module.MAX_BYTES, f"digest is {len(first)} bytes"
document = json.loads(first)
assert document["schema_version"] == module.SCHEMA_VERSION
assert document["generated_at"] == "2026-11-11T04:10:00Z"
assert set(document["source_freshness"]) == {"status_exporter", "loki_gateway"}
assert document["nanobot_activity"]["event_count"] == 3
text = first.decode()
for needle in ("PROMPT", "Vacation", "IMG_1234", "read_file", "shadow", "diary"):
    assert needle not in text, needle
print(f"{len(first)} bytes, {document['nanobot_activity']['event_count']} events")
PY
  ok "fixture digest: $(cat "$work/determinism.log")"
else
  bad "fixture digest is not deterministic or exceeds the bound" "$(tail -5 "$work/determinism.log")"
fi

# ------------------------------------------------------------------ summary
printf 'digest: %d passed, %d failed\n' "$pass" "$fail"
((fail == 0))
