#!/usr/bin/env bash
# Runtime proof of the Access default-deny (SDLC #581).
#
# Picks a hostname nobody has an application for, asks the Cloudflare API which
# application Access would match it to, and then makes a real request to it.
# The default-deny holds when the match is the wildcard application and the
# edge answers with the Access login redirect instead of the origin.
#
# It needs a token that can read Access applications, the zone and DNS. The
# token is absent in this tree by design (`docs/manual-steps.md` step 2), so the
# script skips cleanly and `docs/cloudflare.md` carries the expected output.
#
#   CLOUDFLARE_API_TOKEN=... tests/access/check.sh
#   ACCESS_PROBE_HOST=probe.example.sammohr.dev tests/access/check.sh
#
# Environment:
#   CLOUDFLARE_API_TOKEN    required to run; absent means skip
#   CLOUDFLARE_ACCOUNT_ID   optional, discovered from the token when unset
#   ACCESS_DOMAIN           zone to probe, default sammohr.dev
#   ACCESS_PROBE_HOST       hostname to probe, default probe-<epoch>.<zone>
#   ACCESS_TIMEOUT          curl timeout in seconds, default 20
set -uo pipefail

domain=${ACCESS_DOMAIN:-sammohr.dev}
probe=${ACCESS_PROBE_HOST:-probe-$(date +%s)."$domain"}
timeout=${ACCESS_TIMEOUT:-20}
api_root=https://api.cloudflare.com/client/v4

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

if [[ -z ${CLOUDFLARE_API_TOKEN:-} ]]; then
  cat <<'EOF'
skip tests/access/check.sh: CLOUDFLARE_API_TOKEN is not set.

This is the expected state for this repository: the token lives in the
`cloudflare` Actions environment and in the operator's secret store, never in a
checkout (docs/manual-steps.md step 2). The static half of the assertion runs
without credentials:

  tests/tofu/check.sh

Run the runtime half from a shell that holds the token:

  CLOUDFLARE_API_TOKEN=... tests/access/check.sh

Expected output on a passing zone is in docs/cloudflare.md, "Verifying the
default-deny".
EOF
  exit 0
fi

for tool in curl jq; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "tests/access/check.sh: missing $tool (nix shell nixpkgs#curl nixpkgs#jq)" >&2
    exit 1
  }
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

api() {
  curl -sS --max-time "$timeout" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
    -H 'Content-Type: application/json' \
    "$api_root$1"
}

account_id=${CLOUDFLARE_ACCOUNT_ID:-}
if [[ -z $account_id ]]; then
  account_id=$(api '/accounts?per_page=50' | jq -r '.result[0].id // empty')
fi
[[ $account_id =~ ^[0-9a-f]{32}$ ]] || {
  echo "tests/access/check.sh: no account id; set CLOUDFLARE_ACCOUNT_ID" >&2
  exit 1
}

apps=$(api "/accounts/${account_id}/access/apps?per_page=100")
if [[ $(jq -r '.success' <<<"$apps") != true ]]; then
  bad "Cloudflare API read of Access applications" "$(jq -r '.errors[]?.message' <<<"$apps")"
  printf '\n%s: %d passed, %d failed\n' access "$pass" "$fail"
  exit 1
fi

printf 'probe %s against %s\n\n' "$probe" "$domain"

# Cloudflare picks the application whose domain matches the request; the
# longest domain wins and a wildcard covers exactly one label.
matched=$(jq -r --arg host "$probe" '
  def uri: (.domain // ((.destinations // []) | map(.uri // "") | first // ""));
  [ .result[] | {name: .name, uri: uri} ] as $apps
  | [ $apps[] as $a
      | ($a.uri) as $d
      | if $d == $host then $a
        elif ($d | startswith("*.")) then
          ($d[2:]) as $suffix
          | if ($host | endswith("." + $suffix))
               and (($host | split(".") | length) == (($suffix | split(".") | length) + 1))
            then $a else empty end
        else empty end ]
  | sort_by(.uri | length)
  | last // empty
' <<<"$apps")

if [[ -z $matched ]]; then
  bad "an application matches ${probe}" "no exact and no one-label wildcard application: the hostname is unprotected"
else
  ok "Access matches ${probe} to application \"$(jq -r '.name' <<<"$matched")\""
  if jq -e '.uri | startswith("*.")' >/dev/null <<<"$matched"; then
    ok "the matching application is the wildcard, not an override"
  else
    bad "the matching application is the wildcard, not an override" "matched $(jq -r '.uri' <<<"$matched")"
  fi
fi

# The API result above is what Access should do; this is what the edge does.
body="$work/headers"
code=$(curl -sS --max-time "$timeout" -o /dev/null -D "$body" -w '%{http_code}' "https://${probe}/" 2>"$work/curl.err")
curl_status=$?
if ((curl_status != 0)); then
  bad "request to https://${probe}/" "$(cat "$work/curl.err")"
else
  location=$(grep -i '^location:' "$body" | head -1 | tr -d '\r')
  cf_ray=$(grep -ci '^cf-ray:' "$body")
  printf '     HTTP %s %s\n' "$code" "$location"
  case $code in
    302 | 303)
      if grep -qi 'cloudflareaccess\.com' <<<"$location"; then
        ok "the edge redirects to the Access login instead of the origin"
      else
        bad "the edge redirects to the Access login instead of the origin" "Location: $location"
      fi
      ;;
    401 | 403)
      # A programmatic client gets the denial page instead of a login redirect.
      ok "the edge denies the request (HTTP ${code})"
      ;;
    *)
      bad "the edge answers as Access-protected (got HTTP ${code})" \
        "a 200/404 means the origin answered and Access was not applied; 5xx means the tunnel failed before Access (cf-ray headers: ${cf_ray})"
      ;;
  esac
fi

printf '\n%s: %d passed, %d failed\n' access "$pass" "$fail"
((fail == 0)) || exit 1
