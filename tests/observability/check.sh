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

for tool in yq jq vector python3 git; do
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
eq "$(yq -r '.sinks.loki_raw.tenant_id' "$vector_prod")" "raw" \
  "raw sink writes the raw tenant directly"
if [[ $(yq -r '.sinks.loki_data | has("tenant_id")' "$vector_prod") == "false" ]]; then
  ok "data sink cannot choose a tenant (gateway injects X-Scope-OrgID)"
else
  bad "data sink sets tenant_id; the tenant must come from the gateway"
fi
if [[ $(yq -r '.sinks.loki_data.endpoint' "$vector_prod") == *loki-gateway* ]]; then
  ok "data sink writes through loki-gateway"
else
  bad "data sink does not point at loki-gateway"
fi
eq "$(yq -r '.sinks.loki_data.auth.strategy' "$vector_prod")" "bearer" \
  "data sink authenticates with a bearer token"

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
eq "$(yq -r '.spec.template.spec.containers[0].env[] | select(.name == "LOKI_GATEWAY_TOKEN") | .valueFrom.secretKeyRef.name' \
  clusters/homelab/observability/vector/daemonset.yaml)" \
  "loki-gateway-token" "Vector presents the same token to the gateway"

# --------------------------------------------------------- status exporter RBAC
exporter_dir=clusters/homelab/observability/status-exporter
eq "$(yq -r '.rules | map(.resources | join("+")) | join(" ")' "$exporter_dir/clusterrole.yaml")" \
  "pods deployments+statefulsets" "status exporter reads only pods and workload kinds"
eq "$(yq -r '.rules | map(.verbs | join("+")) | join(" ")' "$exporter_dir/clusterrole.yaml")" \
  "get+list+watch get+list" "status exporter verbs are read-only and exact"
if grep -rq 'kind: ClusterRoleBinding' "$exporter_dir" clusters/homelab/observability/rbac-data.yaml; then
  bad "status exporter has a ClusterRoleBinding (it would read every namespace)"
else
  ok "status exporter has no ClusterRoleBinding"
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

# ------------------------------------------------------------- python unit tests
if python3 -m unittest discover -s tests/observability -p 'test_*.py' \
  >"$work/python-tests.log" 2>&1; then
  ok "python unit tests: $(grep -oE 'Ran [0-9]+ tests' "$work/python-tests.log")"
else
  bad "python unit tests failed" "$(tail -30 "$work/python-tests.log")"
fi

printf '\nobservability: %d passed, %d failed\n' "$pass" "$fail"
((fail == 0)) || exit 1
