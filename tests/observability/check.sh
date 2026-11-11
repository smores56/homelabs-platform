#!/usr/bin/env bash
# Observability pipeline checks. Static only: no cluster, no credentials.
#
#   tests/observability/check.sh
#
# Proves the data-tenant allowlist with Vector's own unit tests (the planted
# Immich line loses its filename and album name), validates the production
# Vector config, asserts that the test topology and the shipped pipeline have not
# drifted, pins the tenant routing, and runs the status-exporter and Loki-gateway
# unit tests. kustomize build and kubeconform are separate CI jobs; they pick up
# every clusters/ tree automatically.
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

for tool in yq jq vector python3 git kustomize; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "missing required tool: $tool" >&2
    echo "nix shell nixpkgs#yq-go nixpkgs#jq nixpkgs#python3 -c tests/observability/check.sh" >&2
    exit 1
  }
done

# Keep the working tree clean: imports in the unit tests would otherwise write
# __pycache__ next to the sources that kustomize embeds.
export PYTHONDONTWRITEBYTECODE=1

vector_prod=clusters/homelab/observability/vector/vector.yaml
vector_test=tests/vector/vector.yaml
vrl_prod=clusters/homelab/observability/vector/data_allowlist.vrl
loki_config=clusters/homelab/observability/loki/loki.yaml
fixture=tests/observability/fixtures/immich-planted.json
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# ------------------------------------------------------- vector unit + validate
if vector test --config-yaml "$vector_test" >"$work/vector-test.log" 2>&1; then
  ok "vector test: $(grep -c '^test ' "$work/vector-test.log") cases (allowlist, routing)"
else
  bad "vector test failed" "$(tail -30 "$work/vector-test.log")"
fi

# `vector validate` opens `file:` remap paths, so the VRL is staged where the
# container mounts it and the config is pointed at that copy.
mkdir -p "$work/etc/vector/vrl"
cp "$vrl_prod" "$work/etc/vector/vrl/data_allowlist.vrl"
sed "s#/etc/vector/vrl/data_allowlist.vrl#$work/etc/vector/vrl/data_allowlist.vrl#" \
  "$vector_prod" >"$work/vector.yaml"
if vector validate --no-environment --skip-healthchecks "$work/vector.yaml" \
  >"$work/vector-validate.log" 2>&1; then
  ok "vector validate: production config loads"
else
  bad "vector validate failed" "$(cat "$work/vector-validate.log")"
fi

# ------------------------------------------------ shipped pipeline cannot drift
# The unit test builds its own topology; these comparisons are what keep the
# test honest when the production config changes.
eq "$(yq -o=json -I=0 '.transforms.tenant_route.source' "$vector_prod")" \
  "$(yq -o=json -I=0 '.transforms.tenant_route.source' "$vector_test")" \
  "test topology uses the shipped tenant_route source"
eq "$(yq -r '.transforms.data_filter.condition' "$vector_prod")" \
  "$(yq -r '.transforms.data_filter.condition' "$vector_test")" \
  "test topology uses the shipped data_filter condition"
eq "$(yq -r '.transforms.raw_filter.condition' "$vector_prod")" \
  "$(yq -r '.transforms.raw_filter.condition' "$vector_test")" \
  "test topology uses the shipped raw_filter condition"
eq "$(yq -r '.transforms.data_allowlist.file' "$vector_prod")" \
  "/etc/vector/vrl/data_allowlist.vrl" \
  "production allowlist transform reads the mounted VRL file"
eq "$(yq -r '.transforms.data_allowlist.file' "$vector_test")" \
  "clusters/homelab/observability/vector/data_allowlist.vrl" \
  "unit tests run the same VRL source file"
eq "$(yq -r '.configMapGenerator[] | select(.name == "vector-vrl") | .files[0]' \
  clusters/homelab/observability/vector/kustomization.yaml)" \
  "data_allowlist.vrl" \
  "the VRL file ships as the vector-vrl ConfigMap"

# The planted line exists twice: as a reviewable fixture and as the unit-test
# input. Comparing the parsed JSON catches an edit to either side.
fixture_json=$(jq -c . "$fixture")
planted=$(yq -r '.tests[0].inputs[0].log_fields.message' "$vector_test")
eq "$(jq -c . <<<"$planted")" "$fixture_json" "the unit test plants the immich-planted.json line"
if jq -e 'has("file") and has("albumName") and has("originalPath")' <<<"$fixture_json" >/dev/null; then
  ok "the fixture carries a filename, an album name and a path"
else
  bad "the fixture does not carry the fields the test claims to drop"
fi

# ------------------------------------------------------------ tenant separation
# Both sinks push to Loki's own write API, which is the only API Vector speaks.
# The gateway is read-only by design (see test_loki_gateway.py), so a sink
# pointed at it would be rejected with 403 and the data tenant would stay empty
# while every freshness field claimed success.
eq "$(yq -r '.sinks.loki_raw.tenant_id' "$vector_prod")" "raw" \
  "raw sink writes the raw tenant directly"
eq "$(yq -r '.sinks.loki_data.tenant_id' "$vector_prod")" "data" \
  "data sink names the data tenant with tenant_id"
eq "$(yq -r '.sinks.loki_data.endpoint' "$vector_prod")" \
  "http://loki.observability.svc.cluster.local:3100" \
  "data sink writes to Loki directly, not through the read-only gateway"
eq "$(yq -r '.sinks.loki_data | has("auth")' "$vector_prod")" "false" \
  "data sink carries no gateway token (the push path is network-scoped)"
eq "$(yq -r '.sinks.loki_data.labels.tier' "$vector_prod")" "data" \
  "data sink labels every line tier=data"

eq "$(yq -r '.auth_enabled' "$loki_config")" "true" "Loki runs with auth_enabled"
eq "$(yq -r '.limits_config.retention_period' "$loki_config")" "168h" \
  "Loki retains 7 days"
eq "$(yq -r '.analytics.reporting_enabled' "$loki_config")" "false" \
  "Loki telemetry reporting is off"

# ------------------------------------------------------------ token contract
eq "$(yq -r '.spec.template.spec.containers[0].env[] | select(.name == "LOKI_GATEWAY_TOKEN") | .valueFrom.secretKeyRef.name' \
  clusters/homelab/observability/loki-gateway/deployment.yaml)" \
  "loki-gateway-token" "gateway reads the ESO-generated token Secret"
eq "$(yq -r '.spec.target.name' clusters/homelab/observability/loki-gateway/eso/externalsecret.yaml)" \
  "loki-gateway-token" "ExternalSecret targets the loki-gateway-token Secret"
eq "$(yq -r '.spec.dataFrom[0].sourceRef.generatorRef.kind' \
  clusters/homelab/observability/loki-gateway/eso/externalsecret.yaml)" \
  "Password" "token comes from an ESO Password generator"
# Vector writes with its own tenant header, so it holds no credential at all.
eq "$(grep -c 'LOKI_GATEWAY_TOKEN' clusters/homelab/observability/vector/daemonset.yaml)" "0" \
  "Vector holds no gateway token (it writes the tenant directly)"
eq "$(grep -rc 'secretName\|secretKeyRef' clusters/homelab/observability/vector/daemonset.yaml)" "0" \
  "Vector mounts no Secret"

# --------------------------------------------------------- status exporter RBAC
# The ClusterRole grants `nodes`, which is a cluster-scoped resource: only a
# ClusterRoleBinding can grant it. Everything else stays namespaced.
exporter_dir=clusters/homelab/observability/status-exporter
eq "$(yq -r '.rules | map(.resources | join("+")) | join(" ")' "$exporter_dir/clusterrole.yaml")" \
  "pods deployments+statefulsets" "status exporter reads only pods and workload kinds"
eq "$(yq -r '.rules | map(.verbs | join("+")) | join(" ")' "$exporter_dir/clusterrole.yaml")" \
  "get+list+watch get+list" "status exporter verbs are read-only and exact"
eq "$(yq -r '.rules | map(.resources | join("+")) | join(" ")' "$exporter_dir/clusterrole-nodes.yaml")" \
  "nodes" "the cluster-scoped read is nodes and nothing else"
eq "$(yq -r '.roleRef.kind + ":" + .roleRef.name' "$exporter_dir/clusterrolebinding.yaml")" \
  "ClusterRole:observability-status-exporter-nodes" \
  "the ClusterRoleBinding names the nodes-only ClusterRole"
eq "$(yq -r '.subjects | map(.kind + ":" + .namespace + ":" + .name) | join(",")' \
  "$exporter_dir/clusterrolebinding.yaml")" \
  "ServiceAccount:observability:status-exporter" \
  "the ClusterRoleBinding binds only observability/status-exporter"
eq "$(grep -c 'kind: ClusterRoleBinding' "$exporter_dir/clusterrolebinding.yaml")" "1" \
  "the exporter has exactly one ClusterRoleBinding"
# A ClusterRoleBinding of the workload role would hand `pods` out cluster-wide,
# so the whole observability tree is rendered and the bindings are enumerated.
kustomize build clusters/homelab/observability >"$work/observability.yaml"
yq -o=json -I=0 '.' "$work/observability.yaml" | jq -s '.' >"$work/observability.json"
eq "$(jq -r '[.[] | select(.kind == "ClusterRoleBinding") | .roleRef.name] | sort | join(",")' \
  "$work/observability.json")" \
  "observability-prometheus-pods,observability-status-exporter-nodes,observability-vector-metadata" \
  "every ClusterRoleBinding in observability names a cluster-scoped role"
eq "$(jq -r '[.[] | select(.kind == "ClusterRoleBinding") | .roleRef.name | select(. == "observability-status-exporter")] | length' \
  "$work/observability.json")" "0" \
  "no ClusterRoleBinding grants the namespaced workload role cluster-wide"
# The binding exists for nodes only: the ClusterRole's pods/workloads rules are
# scoped by the per-namespace RoleBindings below.
if grep -q 'kind: ClusterRoleBinding' clusters/homelab/observability/rbac-data.yaml; then
  bad "a data namespace holds a ClusterRoleBinding"
else
  ok "the data namespaces hold RoleBindings only"
fi
# One RoleBinding per data namespace, exactly the namespaces the exporter is
# configured to scrape (the data tier is one namespace per app).
eq "$(yq -r '.metadata.namespace' clusters/homelab/observability/rbac-data.yaml | grep -v '^---$' | sort | paste -sd, -)" \
  "$(yq -r '.spec.template.spec.containers[0].env[] | select(.name == "DATA_NAMESPACES") | .value | split(",") | sort | join(",")' \
    "$exporter_dir/deployment.yaml")" \
  "status exporter has one RoleBinding per scraped data namespace"
eq "$(yq -r '.subjects[0].namespace' clusters/homelab/observability/rbac-data.yaml | grep -v '^---$' | sort -u)" \
  "observability" "every binding names the observability service account"

# ------------------------------------------------------------ postgres fragment
postgres=clusters/homelab/data/immich/postgres-settings.yaml
if grep -Fq 'log_error_verbosity = terse' "$postgres" &&
  grep -Fq 'log_min_error_statement = panic' "$postgres"; then
  ok "postgres fragment sets terse verbosity and panic statement logging"
else
  bad "postgres fragment is missing a required setting"
fi

# ----------------------------------------------------------------- image pins
if grep -rhoE '^\s+image: \S+' clusters/homelab/observability clusters/homelab/data |
  grep -vE '(:v?[0-9][0-9A-Za-z._-]*(@sha256:[0-9a-f]{64})?|@sha256:[0-9a-f]{64})$' >"$work/unpinned"; then
  bad "an observability image is not pinned to a version or digest" "$(cat "$work/unpinned")"
else
  ok "every observability and data image is version- or digest-pinned"
fi

# ---------------------------------------------------------- network allowlists
# The namespace is default-deny; the render is what proves the allowlist is the
# reviewed set and that nothing added a public egress rule.
network_json="$work/network.json"
jq '[.[] | select(.kind == "NetworkPolicy" and .metadata.namespace == "observability")]' \
  "$work/observability.json" >"$network_json"

eq "$(jq -r '[.[] | select(.metadata.name == "default-deny") | (.spec.policyTypes | sort | join(","))] | join(",")' \
  "$network_json")" "Egress,Ingress" "observability is default-deny in both directions"
eq "$(jq -r '[.[] | .metadata.name] | sort | join(",")' "$network_json")" \
  "allow-dns-egress,default-deny,kubernetes-api-egress,loki-gateway-egress-loki,loki-gateway-from-broker,loki-ingress,prometheus-egress-scrape,status-exporter-from-broker,status-exporter-from-prometheus,vector-egress-loki,vector-ingress-prometheus" \
  "the observability namespace holds exactly the reviewed policy set"
eq "$(jq -r '.[] | select(.metadata.name == "allow-dns-egress") | .spec.egress[0].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"]' \
  "$network_json")" "kube-system" "observability DNS egress targets kube-system only"
eq "$(jq -r '.[] | select(.metadata.name == "loki-ingress") | [.spec.ingress[] | .from[0].podSelector.matchLabels["app.kubernetes.io/name"] + ":" + (.ports[0].port | tostring)] | sort | join(",")' \
  "$network_json")" "loki-gateway:3100,vector:3100" \
  "Loki accepts writes from Vector and reads from the gateway only"
eq "$(jq -r '.[] | select(.metadata.name == "vector-egress-loki") | [.spec.egress[0].to[0].podSelector.matchLabels["app.kubernetes.io/name"], (.spec.egress[0].ports[0].port | tostring)] | join(":")' \
  "$network_json")" "loki:3100" "Vector egress is Loki on 3100 and nothing else"
eq "$(jq -r '.[] | select(.metadata.name == "loki-gateway-egress-loki") | [.spec.egress[0].to[0].podSelector.matchLabels["app.kubernetes.io/name"], (.spec.egress[0].ports[0].port | tostring)] | join(":")' \
  "$network_json")" "loki:3100" "the gateway reaches only Loki on 3100"
eq "$(jq -r '.[] | select(.metadata.name == "prometheus-egress-scrape") | [.spec.egress[0].ports[].port] | sort | map(tostring) | join(",")' \
  "$network_json")" "8686,9090,9101" "Prometheus may scrape only the annotated ports in observability"
eq "$(jq -r '.[] | select(.metadata.name == "prometheus-egress-scrape") | [.spec.egress[1].to[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], (.spec.egress[1].ports[0].port | tostring)] | join(":")' \
  "$network_json")" "flux-system:8080" "Prometheus may scrape the Flux controllers"
eq "$(jq -r '.[] | select(.metadata.name == "status-exporter-from-prometheus") | [.spec.ingress[0].from[0].podSelector.matchLabels["app.kubernetes.io/name"], (.spec.ingress[0].ports[0].port | tostring)] | join(":")' \
  "$network_json")" "prometheus:9101" "the exporter accepts Prometheus on 9101"
eq "$(jq -r '.[] | select(.metadata.name == "status-exporter-from-broker") | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], (.spec.ingress[0].ports[0].port | tostring)] | join(":")' \
  "$network_json")" "broker:9101" "the broker path to the exporter is kept"
eq "$(jq -r '.[] | select(.metadata.name == "loki-gateway-from-broker") | [.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"], (.spec.ingress[0].ports[0].port | tostring)] | join(":")' \
  "$network_json")" "broker:3100" "the broker path to the gateway is kept"
eq "$(jq -r '.[] | select(.metadata.name == "kubernetes-api-egress") | [(.spec.podSelector.matchExpressions[0].values | sort | join("+")), .spec.egress[0].to[0].ipBlock.cidr, (.spec.egress[0].ports[0].port | tostring)] | join(":")' \
  "$network_json")" "prometheus+status-exporter+vector:10.43.0.0/16:443" \
  "API egress is the k3s service range on 443 for the three API clients"
if jq -r '.[] | select(.metadata.name != "allow-dns-egress") | .spec.egress[]? | .to[]? | .ipBlock.cidr // empty' \
  "$network_json" | grep -qx '0.0.0.0/0'; then
  bad "an observability policy allows public egress"
else
  ok "no observability pod reaches the public internet"
fi
if jq -e '.[] | select(.spec.egress[]? | (.to // []) == [])' "$network_json" >/dev/null; then
  bad "an observability egress rule omits its destination"
else
  ok "every observability egress rule names its destination"
fi

# ------------------------------------------------------------- python unit tests
if python3 -m unittest discover -s tests/observability -p 'test_*.py' \
  >"$work/python-tests.log" 2>&1; then
  ok "python unit tests: $(grep -oE 'Ran [0-9]+ tests' "$work/python-tests.log")"
else
  bad "python unit tests failed" "$(tail -30 "$work/python-tests.log")"
fi

printf '\nobservability: %d passed, %d failed\n' "$pass" "$fail"
((fail == 0)) || exit 1
