#!/usr/bin/env bash
# Static assertions for roles/backup. No host, no ZFS, no restic.
#
#   nix-shell -p python3 --run 'bash ansible/roles/backup/tests/check.sh'
set -uo pipefail

role=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
root=$(git -C "$role" rev-parse --show-toplevel)

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL %s\n' "$1"; }
check() { if "$@" >/dev/null 2>&1; then ok "$*"; else bad "$*"; fi; }
refute() { if "$@" >/dev/null 2>&1; then bad "$*"; else ok "not: $*"; fi; }

check grep -q 'AssertPathIsMountPoint={{ mount }}' "$role/templates/snapshot.service.j2"
check grep -q 'backup_pending_host_groups' "$role/templates/snapshot.service.j2"
check grep -q 'RequiresMountsFor=' "$role/templates/snapshot.service.j2"
check grep -q 'zfs_datasets' "$role/templates/snapshot.service.j2"
check grep -q 'backup_mounts' "$role/templates/snapshot.service.j2"
check grep -q 'homelab-snapshot@' "$role/templates/snapshot-request.py.j2"
check grep -q 'homelab-snapshot-request.service' "$role/templates/snapshot.path.j2"

refute grep -rniE 'inmemorydestination|dryrunrunner|fixturesnapshotter' \
  "$role/templates" "$role/defaults"
refute grep -rnE -- '--password([^a-z-]|$)' "$role/templates" "$role/tasks"
refute grep -rnE 'RESTIC_PASSWORD=|password:[[:space:]]+[A-Za-z0-9]' \
  "$role/templates" "$role/tasks" "$role/defaults"

if command -v python3 >/dev/null 2>&1; then
  if python3 - "$root" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "render", root / "scripts" / "render-recovery-groups.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
all_vars, backup_vars = module.load_group_vars()
plan = module.build_host_plan(all_vars, backup_vars)
repositories = {repository["name"]: repository for repository in plan["repositories"]}
assert plan["groups"], "no backup groups"
for group in plan["groups"]:
    assert group["destinations"], f"{group['id']} has no repository"
    assert set(group["destinations"]) <= set(repositories), group["id"]
    for path in group["dataset_paths"].values():
        assert path.startswith("/") and "{snapshot}" in path, path
for repository in repositories.values():
    assert repository["password_file"].startswith("/etc/homelab/secrets/")
forbidden = ("fixture", "inmemorydestination", "dryrunrunner")
text = json.dumps(plan).lower()
assert not any(word in text for word in forbidden), "fixture backend in plan"
print("plan ok")
PY
  then
    ok "every group has a repository, every path is snapshot-scoped"
  else
    bad "backup plan failed static validation"
  fi
else
  printf 'skip python3 missing; plan assertions not run\n'
fi

if command -v python3 >/dev/null 2>&1; then
  if python3 "$root/scripts/render-recovery-groups.py" --check >/dev/null 2>&1; then
    ok "deploy inventory matches the Ansible vars"
  else
    bad "deploy inventory is stale; run scripts/render-recovery-groups.py"
  fi
else
  printf 'skip python3 missing; inventory drift check not run\n'
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
