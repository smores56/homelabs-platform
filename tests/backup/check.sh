#!/usr/bin/env bash
# Offline shape tests for the repository-mirror script (SDLC #583).
#
# The network half (clone, bundle, token accepted by GitHub) needs the PAT and a
# cluster; this checks the half that does not: argument parsing, the owner/repo
# grammar, the exact clone URL, the classic-token refusal, and that neither the
# plan output nor a failure ever contains the token value.
#
#   tests/backup/check.sh
set -uo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root" || exit 1

script=clusters/homelab/platform/backup/github-backup.sh

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

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fine=github_pat_11ABCDEFG0123456789_abcdefghijklmnop
classic=ghp_abcdefghijklmnopqrstuvwxyz0123456789
printf '%s\n' "$fine" >"$work/fine"
printf '%s\n' "$classic" >"$work/classic"

# run <args...>: sets rc and out (stdout+stderr) in the caller.
run() {
  out=$(sh "$script" "$@" 2>&1)
  rc=$?
}

if sh -n "$script"; then ok "script parses (sh -n)"; else bad "script parses (sh -n)"; fi

run --owner smores56 --repo homelabs-platform --print-url
if [[ $rc == 0 && $out == "https://github.com/smores56/homelabs-platform.git" ]]; then
  ok "--print-url builds the platform clone URL"
else
  bad "--print-url builds the platform clone URL" "rc=$rc out=$out"
fi

run --repo homelabs-apps --owner smores56 --print-url
if [[ $rc == 0 && $out == "https://github.com/smores56/homelabs-apps.git" ]]; then
  ok "--print-url builds the apps clone URL"
else
  bad "--print-url builds the apps clone URL" "rc=$rc out=$out"
fi

run --owner smores56 --repo homelabs-platform --print-url --token-file "$work/missing"
if [[ $rc == 0 && $out == "https://github.com/smores56/homelabs-platform.git" ]]; then
  ok "--print-url reads no token file"
else
  bad "--print-url reads no token file" "rc=$rc out=$out"
fi

for spec in "missing-owner:--repo homelabs-platform" "missing-repo:--owner smores56" \
  "slash-owner:--owner a/b --repo r" "dotdot-owner:--owner .. --repo r" \
  "leading-dash-owner:--owner -x --repo r" "slash-repo:--owner o --repo a/b" \
  "dotdot-repo:--owner o --repo .." "empty-repo:--owner o --repo "; do
  name=${spec%%:*}
  # shellcheck disable=SC2086  # the argument list is intended to split
  run ${spec#*:} --print-url
  if [[ $rc != 0 ]]; then ok "rejects $name"; else bad "rejects $name" "rc=$rc out=$out"; fi
done

run --owner smores56 --repo homelabs-platform --print-url --out relative
if [[ $rc != 0 ]]; then ok "rejects a relative --out"; else bad "rejects a relative --out" "rc=$rc out=$out"; fi

run --owner smores56 --repo homelabs-platform --base http://github.com --print-url
if [[ $rc != 0 && $out == *"https://github.com"* ]]; then
  ok "rejects a non-TLS base"
else
  bad "rejects a non-TLS base" "rc=$rc out=$out"
fi

run --owner smores56 --repo homelabs-platform --base https://github.com/ --print-url
if [[ $rc != 0 ]]; then ok "rejects a base with a trailing slash"; else bad "rejects a base with a trailing slash"; fi

run --owner smores56 --repo homelabs-platform --base https://ghe.example.com --print-url
if [[ $rc != 0 ]]; then ok "rejects a GHES hostname"; else bad "rejects a GHES hostname"; fi

run --bogus
if [[ $rc == 2 && $out == *"usage:"* ]]; then ok "rejects an unknown flag with usage"; else bad "rejects an unknown flag with usage" "rc=$rc out=$out"; fi

run --owner smores56 --repo homelabs-platform --dry-run --token-file "$work/fine" --out "$work/out"
if [[ $rc == 0 ]] &&
  grep -Fq "url=https://github.com/smores56/homelabs-platform.git" <<<"$out" &&
  grep -Fq "mirror=$work/out/mirror/smores56/homelabs-platform.git" <<<"$out" &&
  grep -Fq "bundle=$work/out/bundles/smores56/homelabs-platform.bundle" <<<"$out" &&
  grep -Fq "token=fine-grained" <<<"$out"; then
  ok "--dry-run prints the mirror and bundle paths"
else
  bad "--dry-run prints the mirror and bundle paths" "rc=$rc out=$out"
fi

if [[ $out == *"$fine"* ]]; then bad "--dry-run never prints the token"; else ok "--dry-run never prints the token"; fi

run --owner smores56 --repo homelabs-platform --dry-run --token-file "$work/classic"
if [[ $rc != 0 && $out == *"fine-grained"* ]]; then
  ok "refuses a classic ghp_ token"
else
  bad "refuses a classic ghp_ token" "rc=$rc out=$out"
fi
if [[ $out == *"$classic"* ]]; then bad "classic-token failure never prints the value"; else ok "classic-token failure never prints the value"; fi

run --owner smores56 --repo homelabs-platform --dry-run --token-file "$work/missing"
if [[ $rc != 0 && $out == *"token file not found"* ]]; then
  ok "refuses a missing token file"
else
  bad "refuses a missing token file" "rc=$rc out=$out"
fi

run --owner smores56 --repo homelabs-platform --dry-run --token-file "$work/fine"
if [[ $rc == 0 && $out == *"mirror=/backup/mirror/smores56/homelabs-platform.git"* ]]; then
  ok "the container default --out is /backup"
else
  bad "the container default --out is /backup" "rc=$rc out=$out"
fi

# The CronJob installs the same file into a ConfigMap; a stray edit to one copy
# is caught by the kustomize build in CI, and this asserts the mount path.
if grep -Fq "configMap:" clusters/homelab/platform/backup/cronjob.yaml &&
  grep -Fq "mountPath: /opt/github-backup" clusters/homelab/platform/backup/cronjob.yaml; then
  ok "the CronJob mounts the script from a ConfigMap"
else
  bad "the CronJob mounts the script from a ConfigMap"
fi

printf '\n%s: %d passed, %d failed\n' backup "$pass" "$fail"
((fail == 0)) || exit 1
