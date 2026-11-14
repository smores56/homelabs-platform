#!/usr/bin/env bash
# Static checks for the Ubuntu host provisioning tree.
#
#   nix-shell -p ansible yamllint --run 'ansible/tests/check.sh'
#
# No host is contacted and no network call is made. Missing tools are skipped
# with a warning rather than failing the run.
set -uo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root/ansible" || exit 1

pass=0
fail=0
skip=0

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

skipped() {
  skip=$((skip + 1))
  printf 'skip %s\n' "$1"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

if have ansible-playbook; then
  for playbook in playbooks/*.yml; do
    if output=$(ansible-playbook --syntax-check -i inventory/example.yml "$playbook" 2>&1); then
      ok "syntax $playbook"
    else
      bad "syntax $playbook" "$output"
    fi
  done
else
  skipped "ansible-playbook not found; syntax checks not run"
fi

if have yamllint; then
  if output=$(yamllint -c "$root/.yamllint.yaml" . 2>&1); then
    ok "yamllint"
  else
    bad "yamllint" "$output"
  fi
else
  skipped "yamllint not found; YAML lint not run"
fi

output=$(grep -rnE '^[[:space:]]*ignore_errors:' --include='*.yml' . 2>/dev/null || true)
if [[ -n $output ]]; then
  bad "no ignore_errors" "$output"
else
  ok "no ignore_errors"
fi

# The user module's `shell:` parameter and quoted templates are legitimate;
# only an unquoted/block shell module key is a violation.
output=$(grep -rnE '^[[:space:]]*(ansible\.builtin\.)?shell:' --include='*.yml' . 2>/dev/null \
  | grep -vE 'shell:[[:space:]]*["{]' || true)
if [[ -n $output ]]; then
  bad "no shell tasks" "$output"
else
  ok "no shell tasks"
fi

printf '\n%d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[[ $fail -eq 0 ]]
