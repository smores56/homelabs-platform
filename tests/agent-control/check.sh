#!/usr/bin/env bash
# Unit tests for the platform-owned agent-control service
# (clusters/homelab/agent-control). No cluster and no credentials: the route
# allowlist, the log filter, the Secrets/ConfigMaps guard, the tail clamp and
# the audit-line shape are pure functions, and the Control class runs against a
# fake API client that fails on any path the RBAC scope forbids.
#
#   tests/agent-control/check.sh
set -uo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root" || exit 1

command -v python3 >/dev/null 2>&1 || {
  echo "missing required tool: python3" >&2
  echo "nix shell nixpkgs#python312 -c tests/agent-control/check.sh" >&2
  exit 1
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Keep the working tree clean: an import would otherwise write __pycache__.
export PYTHONDONTWRITEBYTECODE=1

if python3 -m unittest discover -s tests/agent-control -p 'test_*.py' \
  >"$work/tests.log" 2>&1; then
  echo "ok   agent-control unit tests: $(grep -oE 'Ran [0-9]+ tests' "$work/tests.log")"
else
  echo "FAIL agent-control unit tests" >&2
  cat "$work/tests.log" >&2
  exit 1
fi
