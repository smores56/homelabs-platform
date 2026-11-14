#!/usr/bin/env bash
# Static assertions for roles/restic. No repository is touched.
#
#   bash ansible/roles/restic/tests/check.sh
set -uo pipefail

role=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL %s\n' "$1"; }
check() { if "$@" >/dev/null 2>&1; then ok "$*"; else bad "$*"; fi; }
refute() { if "$@" >/dev/null 2>&1; then bad "$*"; else ok "not: $*"; fi; }

check grep -qE '^restic_version: "[0-9]+\.[0-9]+\.[0-9]+"$' "$role/defaults/main.yml"
check grep -qE '^[[:space:]]+(amd64|arm64): [0-9a-f]{64}$' "$role/defaults/main.yml"
check grep -q 'checksum: "sha256:{{ restic_checksums\[restic_arch\] }}"' "$role/tasks/main.yml"
check grep -q 'restic_version not in' "$role/tasks/main.yml"

check grep -q 'cat' "$role/tasks/main.yml"
check grep -q 'when: item.rc != 0' "$role/tasks/main.yml"
check grep -q 'init' "$role/tasks/main.yml"

check grep -q -- '--keep-daily' "$role/templates/restic-retention.py.j2"
check grep -q -- '--keep-weekly' "$role/templates/restic-retention.py.j2"
check grep -q -- '--keep-monthly' "$role/templates/restic-retention.py.j2"
check grep -q -- '"--prune"' "$role/templates/restic-retention.py.j2"
check grep -q -- '"--host"' "$role/templates/restic-retention.py.j2"
check grep -q -- '"--tag"' "$role/templates/restic-retention.py.j2"
check grep -q -- '--password-file' "$role/templates/restic-retention.py.j2"

check grep -q 'unlock' "$role/files/lock-drill.sh"
check grep -q 'fresh lock was removed' "$role/files/lock-drill.sh"
check grep -q 'expired lock survived' "$role/files/lock-drill.sh"
refute grep -qE 'unlock[^#]*--remove-all' "$role/files/lock-drill.sh"

check grep -q "stat.mode == '0600'" "$role/tasks/main.yml"
check grep -q "stat.pw_name == 'root'" "$role/tasks/main.yml"
check grep -q 'mode: "0700"' "$role/tasks/main.yml"

refute grep -rnE -- '--password([^a-z-]|$)' "$role/tasks" "$role/templates" "$role/files"
refute grep -rnE '\bcopy\b' "$role/templates" "$role/files"
refute grep -rnE 'RESTIC_PASSWORD=' "$role/tasks" "$role/templates" "$role/files"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
