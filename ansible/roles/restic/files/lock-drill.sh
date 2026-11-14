#!/usr/bin/env bash
# Prove restic lock expiry semantics on a throwaway local repository.
#
# A fresh lock must survive `restic unlock`; a lock older than restic's stale
# threshold must be removed. This is the check the design requires before
# production retention (forget --prune) is enabled: never --remove-all, never
# deleting a lock another process may still hold.
#
#   /usr/local/sbin/restic-lock-drill [stale-minutes]
#
# No network, no credentials: a temporary repository and password file.
set -euo pipefail

restic=${RESTIC:-/usr/local/bin/restic}
stale_minutes=${1:-35}
[[ $stale_minutes -gt 30 ]] || { echo "stale-minutes must exceed restic's 30 minute threshold" >&2; exit 2; }

work=$(mktemp -d)
password=$(mktemp)
trap 'rm -rf "$work" "$password"' EXIT
printf 'lock-drill\n' >"$password"
chmod 0600 "$password"

export RESTIC_PASSWORD_FILE="$password"

"$restic" -q init --repo "$work"
"$restic" -q backup --repo "$work" "$password" >/dev/null
mkdir -p "$work/locks"

write_lock() {
  local path=$1 stamp=$2
  printf '{"time":"%s","exclusive":false,"hostname":"lock-drill.invalid","username":"drill","pid":999999,"uid":0,"gid":0}' \
    "$stamp" >"$path"
}

fresh="$work/locks/$(printf 'a%.0s' {1..64})"
expired="$work/locks/$(printf 'b%.0s' {1..64})"
write_lock "$fresh" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_lock "$expired" "$(date -u -d "-$((stale_minutes + 5)) minutes" +%Y-%m-%dT%H:%M:%SZ)"

"$restic" -q unlock --repo "$work"

[[ -f $fresh ]] || { echo "FAIL: a fresh lock was removed" >&2; exit 1; }
[[ ! -f $expired ]] || { echo "FAIL: an expired lock survived unlock" >&2; exit 1; }

printf 'ok: fresh lock kept, %s-minute-old lock cleared\n' "$((stale_minutes + 5))"
