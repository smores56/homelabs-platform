#!/usr/bin/env bash
# Static checks for the Cloudflare OpenTofu config (SDLC #581).
#
# No credentials, no API calls, no state: it reads `tofu/*.tf` as text and
# asserts the reviewed shape of the zone. Runtime behaviour (a hostname with no
# application really being blocked) is tests/access/check.sh, which needs a
# token.
#
#   tests/tofu/check.sh
# shellcheck disable=SC2016  # patterns match HCL text: ${...} is literal, not a shell expansion
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

# has <description> <pattern> : pattern must appear in the whole config.
has() {
  if grep -Eq -- "$2" "$work/code.tf"; then ok "$1"; else bad "$1" "no match for /$2/ in tofu/*.tf"; fi
}
hasnt() {
  if grep -Eq -- "$2" "$work/code.tf"; then bad "$1" "matched /$2/ in tofu/*.tf"; else ok "$1"; fi
}
eq() {
  if [[ $1 == "$2" ]]; then ok "$3"; else bad "$3" "got '$1' want '$2'"; fi
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Marker comment lines are file boundaries; a match is reported with its file.
for f in tofu/*.tf; do
  printf '# %s\n' "$f"
  cat "$f"
done >"$work/all.tf"

# Comment lines are dropped for the assertions: a comment naming a retired
# resource or a deprecated argument must not read as one being present.
grep -Ev '^[[:space:]]*#' "$work/all.tf" >"$work/code.tf"

# extract <type.name> : prints the resource block with that address.
extract() {
  awk -v want="$1" '
    /^resource "/ {
      n = split($0, parts, "\"")
      inblock = (n >= 4 && parts[2] "." parts[4] == want)
    }
    inblock { print }
    inblock && /^}/ { inblock = 0 }
  ' "$work/code.tf"
}

command -v grep >/dev/null 2>&1 || {
  echo "missing grep" >&2
  exit 1
}

echo "== toolchain"
if command -v tofu >/dev/null 2>&1; then
  if tofu fmt -check -recursive tofu; then ok "tofu fmt -check"; else bad "tofu fmt -check" "run: tofu fmt -recursive tofu"; fi
  if (cd tofu && tofu init -backend=false -input=false >/dev/null && tofu validate >"$work/validate.log" 2>&1); then
    ok "tofu init -backend=false + validate"
  else
    bad "tofu init -backend=false + validate" "$(tail -5 "$work/validate.log")"
  fi
else
  printf 'skip tofu fmt/validate (tofu not on PATH; CI runs both)\n'
fi

echo "== default-deny"
wildcard_count=$(grep -Ec '^[[:space:]]*uri[[:space:]]*=[[:space:]]*"\*\.' "$work/code.tf")
eq "$wildcard_count" "1" "exactly one Access destination is a wildcard"

wildcard_app=$(extract cloudflare_zero_trust_access_application.wildcard)
if [[ -n $wildcard_app ]]; then ok "the wildcard destination belongs to cloudflare_zero_trust_access_application.wildcard"; else bad "wildcard Access application exists"; fi
if grep -qF 'uri  = "*.${var.domain}"' <<<"$wildcard_app"; then ok "wildcard application covers *.<zone>"; else bad "wildcard application covers *.<zone>"; fi
if grep -q 'cloudflare_zero_trust_access_policy.email.id' <<<"$wildcard_app"; then ok "wildcard application allows the owner email"; else bad "wildcard application allows the owner email"; fi
if grep -q 'cloudflare_zero_trust_access_policy.bypass.id' <<<"$wildcard_app"; then bad "wildcard application has no bypass policy"; else ok "wildcard application has no bypass policy"; fi
if grep -q 'cloudflare_zero_trust_access_policy.service_auth.id' <<<"$wildcard_app"; then bad "wildcard application grants no service-token access"; else ok "wildcard application grants no service-token access"; fi

if grep -q 'decision   = "bypass"' "$work/code.tf"; then ok "a bypass policy exists"; else bad "a bypass policy exists"; fi
bypass_uses=$(grep -c 'cloudflare_zero_trust_access_policy.bypass.id' "$work/code.tf")
eq "$bypass_uses" "1" "the bypass policy is attached to exactly one resource (the public set)"

public_app=$(extract cloudflare_zero_trust_access_application.public)
if grep -q 'for_each = local.public_apps' <<<"$public_app"; then ok "public applications iterate local.public_apps"; else bad "public applications iterate local.public_apps"; fi
for host in recovery photos music opds; do
  if grep -qE "^[[:space:]]*\"${host}\",$" "$work/code.tf"; then ok "public application: $host"; else bad "public application: $host"; fi
done

echo "== protected applications"
for app in ssh memory agent; do
  block=$(extract "cloudflare_zero_trust_access_application.${app}")
  if [[ -z $block ]]; then
    bad "application ${app} exists"
    continue
  fi
  ok "application ${app} exists"
  if grep -q 'cloudflare_zero_trust_access_policy.service_auth.id' <<<"$block"; then ok "${app} accepts a service token"; else bad "${app} accepts a service token"; fi
  if grep -q 'cloudflare_zero_trust_access_policy.email.id' <<<"$block"; then ok "${app} allows the owner email"; else bad "${app} allows the owner email"; fi
done

# nanobot.sammohr.dev is retired in favour of agent.sammohr.dev (SDLC #583).
if [[ -n $(extract cloudflare_zero_trust_access_application.nanobot) ]]; then
  bad "no nanobot application remains"
else
  ok "no nanobot application remains"
fi

# Kanbei is a public hostname behind Access (SDLC #586). The private WARP route
# is retired: it never got a client past the edge certificate, and a public
# hostname needs no client component.
echo "== kanbei access application"
if [[ -n $(extract cloudflare_zero_trust_network_hostname_route.kanbei) ]]; then
  bad "the private kanbei hostname route is gone"
else
  ok "the private kanbei hostname route is gone"
fi
kanbei_app=$(extract cloudflare_zero_trust_access_application.kanbei)
if [[ -z $kanbei_app ]]; then
  bad "kanbei Access application exists"
else
  ok "kanbei Access application exists"
  if grep -qF 'uri  = "kanbei.${var.domain}"' <<<"$kanbei_app"; then ok "kanbei publishes kanbei.<zone>"; else bad "kanbei publishes kanbei.<zone>"; fi
  if grep -qF 'type = "public"' <<<"$kanbei_app"; then ok "kanbei destination is public"; else bad "kanbei destination is public"; fi
  if grep -q 'cloudflare_zero_trust_access_policy.email.id' <<<"$kanbei_app"; then ok "kanbei allows the owner email"; else bad "kanbei allows the owner email"; fi
  # The account service-auth decision accepts any valid token in the account
  # (the agent's included), so the app must attach exactly the owner policy.
  eq "$(grep -c 'cloudflare_zero_trust_access_policy\.' <<<"$kanbei_app")" "1" \
    "kanbei attaches exactly one Access policy"
  if grep -q 'cloudflare_zero_trust_access_policy.service_auth.id' <<<"$kanbei_app"; then
    bad "kanbei has no service-token policy"
  else
    ok "kanbei has no service-token policy"
  fi
  if grep -q 'cloudflare_zero_trust_access_policy.bypass.id' <<<"$kanbei_app"; then bad "kanbei has no bypass policy"; else ok "kanbei has no bypass policy"; fi
  if grep -qF 'prevent_destroy = true' <<<"$kanbei_app"; then ok "kanbei application is protected from destroy"; else bad "kanbei application is protected from destroy"; fi
  if grep -qF 'allow_authenticate_via_warp = true' <<<"$kanbei_app"; then ok "kanbei lets a WARP client skip the login"; else bad "kanbei lets a WARP client skip the login"; fi
fi
hasnt "kanbei has no public DNS record" '^[[:space:]]*name[[:space:]]*=[[:space:]]*"kanbei\.'
hasnt "kanbei has no tunnel ingress rule" 'kanbei[[:space:]]*=[[:space:]]*"http'
if [[ -n $(extract cloudflare_dns_record.kanbei) ]]; then
  bad "kanbei has no cloudflare_dns_record"
else
  ok "kanbei has no cloudflare_dns_record"
fi

# The operator-facing half of the same contract: the state-rm order for a
# prevent_destroy object and the service-token mint step.
if grep -qF 'state rm cloudflare_zero_trust_access_application.nanobot' docs/manual-steps.md; then
  ok "manual steps remove the nanobot state entry first"
else
  bad "manual steps remove the nanobot state entry first"
fi
if grep -qF 'kanbei.sammohr.dev' docs/manual-steps.md && grep -qiF 'service auth' docs/manual-steps.md; then
  ok "manual steps cover the Kanbei route and service token"
else
  bad "manual steps cover the Kanbei route and service token"
fi

echo "== retired hostnames"
hasnt "no llm application" 'cloudflare_zero_trust_access_application" "llm"'
hasnt "no chat application" 'cloudflare_zero_trust_access_application" "chat"'
hasnt "no llm destination" 'uri[[:space:]]*=[[:space:]]*"llm\.'
hasnt "no chat destination" 'uri[[:space:]]*=[[:space:]]*"chat\.'
hasnt "no nanobot destination" 'uri[[:space:]]*=[[:space:]]*"nanobot\.'

echo "== CAA"
caa=$(grep -cE '^[[:space:]]*type[[:space:]]*=[[:space:]]*"CAA"' "$work/code.tf")
eq "$caa" "2" "two CAA records (issue and issuewild)"
has 'CAA issue carries accounturi' 'tag[[:space:]]*=[[:space:]]*"issue"' 
has 'CAA issuewild carries accounturi' 'tag[[:space:]]*=[[:space:]]*"issuewild"'
accounturi=$(grep -c 'accounturi=\${local.acme_account_uri}' "$work/code.tf")
eq "$accounturi" "2" "both CAA records pin the ACME account"
has 'acme_account_uri derives from the account id variable' 'acme_account_uri = "\$\{trimsuffix\(var\.acme_directory, "/directory"\)\}/acme/acct/\$\{var\.acme_account_id\}"'
has 'acme_account_id is a variable' 'variable "acme_account_id"'
has 'the CAA pin is conditional on the account id' 'count[[:space:]]*=[[:space:]]*var\.acme_account_id == "" \? 0 : 1'
has 'an empty acme_account_id is valid' 'var\.acme_account_id == "" || can\(regex'

echo "== tunnels"
has 'host tunnel CNAME comes from the host tunnel variable' 'host_tunnel_cname[[:space:]]*=[[:space:]]*"\$\{var\.host_tunnel_id\}\.cfargotunnel\.com"'
wildcard_dns=$(extract cloudflare_dns_record.wildcard)
if grep -q 'content = local.host_tunnel_cname' <<<"$wildcard_dns"; then ok "wildcard DNS points at the host tunnel"; else bad "wildcard DNS points at the host tunnel"; fi
if grep -q 'proxied = true' <<<"$wildcard_dns"; then ok "wildcard DNS is proxied"; else bad "wildcard DNS is proxied"; fi
cluster_dns=$(extract cloudflare_dns_record.cluster)
if grep -q 'for_each = toset(local.cluster_hosts)' <<<"$cluster_dns"; then ok "cluster DNS follows the derived host set"; else bad "cluster DNS follows the derived host set"; fi
has 'cluster_hosts defaults to the ingress map' 'var\.cluster_hosts == null \? sort\(keys\(local\.cluster_ingress\)\) : var\.cluster_hosts'
if grep -q 'content = local.cluster_tunnel_cname' <<<"$cluster_dns"; then ok "cluster DNS points at the cluster tunnel"; else bad "cluster DNS points at the cluster tunnel"; fi
if grep -q 'condition     = var.cluster_tunnel_id != ""' <<<"$cluster_dns"; then ok "cluster DNS refuses an empty tunnel id"; else bad "cluster DNS refuses an empty tunnel id"; fi
has 'cluster_tunnel_id is a variable' 'variable "cluster_tunnel_id"'
hasnt "no tunnel UUID is authored in the config" '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

# The cluster tunnel's hostname config lives here, not in a dashboard (SDLC #583).
echo "== cluster tunnel ingress"
cluster_cfg=$(extract cloudflare_zero_trust_tunnel_cloudflared_config.cluster)
if [[ -z $cluster_cfg ]]; then
  bad "cluster tunnel config exists"
else
  ok "cluster tunnel config exists"
  if grep -qF 'tunnel_id  = var.cluster_tunnel_id' <<<"$cluster_cfg"; then ok "cluster config targets the cluster tunnel"; else bad "cluster config targets the cluster tunnel"; fi
  if grep -qF 'count = var.cluster_tunnel_id == "" ? 0 : 1' <<<"$cluster_cfg"; then ok "cluster config tolerates a missing tunnel"; else bad "cluster config tolerates a missing tunnel"; fi
fi
for pair in 'agent=http://nanobot.tenant-apps.svc.cluster.local:80' \
            'photos=http://immich-server.immich.svc.cluster.local:2283' \
            'music=http://navidrome.navidrome.svc.cluster.local:4533' \
            'opds=http://opdshelf.opdshelf.svc.cluster.local:8083' \
            'memory=http://memory-mcp.graphiti.svc.cluster.local:8182'; do
  name=${pair%%=*}
  url=${pair#*=}
  # `tofu fmt` aligns the `=`, so match the mapping rather than one spacing.
  if grep -qE "^[[:space:]]*${name}[[:space:]]*=[[:space:]]*\"${url//./\\.}\"" "$work/code.tf"; then
    ok "$name maps to its in-cluster Service"
  else
    bad "$name maps to its in-cluster Service"
  fi
done
if grep -qF 'http_status:404' "$work/code.tf"; then ok "cluster ingress ends with a 404 catch-all"; else bad "cluster ingress ends with a 404 catch-all"; fi
if grep -qF 'contains(keys(local.cluster_ingress), host)' "$work/code.tf"; then
  ok "cluster DNS and ingress must agree"
else
  bad "cluster DNS and ingress must agree"
fi
if [[ -f tofu/terraform.tfvars ]]; then ok "committed tfvars exists"; else bad "committed tfvars exists"; fi
for key in account_id domain email host_tunnel_id cluster_tunnel_id; do
  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" tofu/terraform.tfvars 2>/dev/null; then
    ok "tfvars sets $key"
  else
    bad "tfvars sets $key"
  fi
done
if grep -qiE 'token|secret|password' tofu/terraform.tfvars 2>/dev/null; then
  bad "tfvars carries no credential"
else
  ok "tfvars carries no credential"
fi
if [[ -x scripts/tofu-cloudflare.sh ]]; then ok "zone apply wrapper is executable"; else bad "zone apply wrapper is executable"; fi
if grep -qF "sops -d --extract" scripts/tofu-cloudflare.sh 2>/dev/null; then
  ok "zone apply wrapper reads the credential from SOPS"
else
  bad "zone apply wrapper reads the credential from SOPS"
fi
if grep -qF '/var/lib/cloudflare-tofu' scripts/tofu-cloudflare.sh 2>/dev/null; then
  ok "zone apply wrapper refuses the legacy state directory"
else
  bad "zone apply wrapper refuses the legacy state directory"
fi
# The identifiers live in tofu/terraform.tfvars, and the host set follows the
# ingress map, so an apply needs no TF_VAR_ exports.
if grep -qF 'TF_VAR_cluster_hosts' docs/manual-steps.md; then
  bad "manual steps do not export cluster_hosts"
else
  ok "manual steps do not export cluster_hosts"
fi

echo "== preserved objects"
for addr in cloudflare_pages_project.recovery cloudflare_pages_domain.recovery \
  cloudflare_dns_record.recovery cloudflare_r2_bucket_cors.restic \
  cloudflare_notification_policy.tunnel_health cloudflare_notification_policy.service_token_expiry \
  cloudflare_ruleset.dav_rate_limit cloudflare_zero_trust_access_policy.email \
  cloudflare_zero_trust_access_policy.service_auth; do
  if [[ -n $(extract "$addr") ]]; then ok "preserved: $addr"; else bad "preserved: $addr"; fi
done
has 'tunnel health covers both tunnels' 'tunnel_id = compact\(\[var\.host_tunnel_id, var\.cluster_tunnel_id\]\)'
has 'rate limit still covers the three public apps' 'http\.host eq \\"music\.\$\{var\.domain\}\\".*photos.*opds'
# The receiver is excluded on purpose: a shared per-IP budget would drop a
# legitimate delivery burst, and the capability URL plus HMAC is the gate.
if grep -q 'agent-hooks' <<<"$(extract cloudflare_ruleset.dav_rate_limit)"; then
  bad "the rate-limit rule does not cover the webhook receiver"
else
  ok "the rate-limit rule does not cover the webhook receiver"
fi

echo "== state backend"
# The state lives in R2 (SDLC #622). The block has to be the S3 shim pointed at
# this account's R2 endpoint, with no credential in it, and the apply path has to
# stay a reviewed dispatch rather than an apply on push.
backend=tofu/backend.tf
if [[ -f $backend ]] && grep -qE '^[[:space:]]*backend[[:space:]]+"s3"' "$backend"; then
  ok "the state backend is the S3-compatible one"
else
  bad "the state backend is the S3-compatible one" "tofu/backend.tf"
fi
eq "$(grep -cE '^[[:space:]]*backend[[:space:]]+"' "$work/code.tf")" "1" \
  "there is exactly one backend block"
account_id=$(sed -nE 's/^[[:space:]]*account_id[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' tofu/terraform.tfvars)
if grep -qE "^[[:space:]]*s3[[:space:]]*=[[:space:]]*\"https://${account_id}\.r2\.cloudflarestorage\.com\"" "$backend"; then
  ok "the backend endpoint is this account's R2 endpoint"
else
  bad "the backend endpoint is this account's R2 endpoint" "expected https://${account_id}.r2.cloudflarestorage.com"
fi
eq "$(sed -nE 's/^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$backend")" \
  "homelabs-tofu-state" "the state bucket is the reviewed one"
eq "$(sed -nE 's/^[[:space:]]*key[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$backend")" \
  "cloudflare/terraform.tfstate" "the state key names the zone's state"
for flag in skip_credentials_validation skip_region_validation skip_requesting_account_id skip_s3_checksum use_path_style; do
  if grep -qE "^[[:space:]]*${flag}[[:space:]]*=[[:space:]]*true" "$backend"; then
    ok "backend sets $flag"
  else
    bad "backend sets $flag"
  fi
done
if grep -qE '(access_key|secret_key|token)[[:space:]]*=' "$backend"; then
  bad "the backend block carries no credential" "credentials come from the environment"
else
  ok "the backend block carries no credential"
fi
if grep -qE 'encryption[[:space:]]*\{' "$work/code.tf"; then
  bad "no client-side state encryption without a key store" "R2 encrypts at rest; state holds identifiers only"
else
  ok "no client-side state encryption without a key store"
fi
# The apply path: dispatch only, no auto-approve, and a detailed-exitcode gate.
apply=.github/workflows/tofu-apply.yml
if grep -qE '^[[:space:]]*push:' "$apply"; then
  bad "tofu-apply does not run on push"
else
  ok "tofu-apply does not run on push"
fi
if grep -q 'workflow_dispatch' "$apply"; then ok "tofu-apply is dispatched by hand"; else bad "tofu-apply is dispatched by hand"; fi
if grep -q -- '-auto-approve' "$apply"; then
  bad "no -auto-approve in the apply workflow"
else
  ok "no -auto-approve in the apply workflow"
fi
if grep -q -- '-detailed-exitcode' "$apply"; then
  ok "the apply workflow gates on plan -detailed-exitcode"
else
  bad "the apply workflow gates on plan -detailed-exitcode"
fi
if grep -qE 'apply:[[:space:]]*true|inputs\.apply' "$apply"; then
  ok "apply runs only when the dispatch asks for it"
else
  bad "apply runs only when the dispatch asks for it"
fi
if git ls-files | grep -q '\.tfstate'; then
  bad "no state file is tracked"
else
  ok "no state file is tracked"
fi
hasnt "no token variable" 'variable "[^"]*token'
hasnt "no API token attribute" '(api_token|api-key|apikey)[[:space:]]*='
hasnt "no bearer secret literal" 'Bearer [A-Za-z0-9]'
if grep -rniE 'cloudflare[_-]?(api[_-])?token' clusters/ | grep -q .; then
  bad "no cluster manifest carries a Cloudflare token"
else
  ok "no cluster manifest carries a Cloudflare token"
fi
if grep -rniE 'cloudflare[_-]?(api[_-])?token' clusters/ | grep -qi nanobot; then
  bad "nanobot holds no Cloudflare token"
else
  ok "nanobot holds no Cloudflare token"
fi

echo "== provider pin and workflows"
has 'provider is pinned to v5' 'version = "~> 5\.0"'
hasnt "no v4 record resource" 'resource "cloudflare_record"'
hasnt "no deprecated self_hosted_domains" 'self_hosted_domains'
if scripts/check-action-pins.sh >"$work/pins.log" 2>&1; then ok "Actions stay SHA-pinned (scripts/check-action-pins.sh)"; else bad "Actions stay SHA-pinned (scripts/check-action-pins.sh)" "$(cat "$work/pins.log")"; fi
if grep -q 'tests/tofu/check.sh' .github/workflows/ci.yml; then ok "ci.yml runs this check"; else bad "ci.yml runs this check"; fi

printf '\n%s: %d passed, %d failed\n' tofu "$pass" "$fail"
((fail == 0)) || exit 1
