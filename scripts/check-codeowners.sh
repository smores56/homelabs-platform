#!/usr/bin/env bash
# Keeps CODEOWNERS and docs/kanbei-critical.md in agreement. CODEOWNERS is the
# enforcement point but cannot carry severity, so the doc is the human-readable
# list; this check fails if either side drifts.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

codeowners=""
for candidate in CODEOWNERS .github/CODEOWNERS docs/CODEOWNERS; do
  if [[ -f $candidate ]]; then
    codeowners="$candidate"
    break
  fi
done
[[ -n $codeowners ]] || { echo "no CODEOWNERS file found" >&2; exit 1; }

mapfile -t patterns < <(awk '!/^[[:space:]]*(#|$)/ {print $1}' "$codeowners")
mapfile -t critical < <(awk -F'`' '/^\| `/ {print $2}' docs/kanbei-critical.md)

status=0
has_pattern() {
  local wanted="$1" pattern
  for pattern in "${patterns[@]}"; do
    [[ $pattern == "$wanted" ]] && return 0
  done
  echo "$codeowners: missing owner entry for '$wanted'" >&2
  return 1
}

has_pattern '*' || status=1
if ((${#critical[@]} == 0)); then
  echo "docs/kanbei-critical.md lists no patterns" >&2
  status=1
fi
for pattern in "${critical[@]}"; do
  has_pattern "$pattern" || status=1
done

if ((status != 0)); then
  echo "CODEOWNERS and docs/kanbei-critical.md disagree" >&2
  exit 1
fi

echo "CODEOWNERS covers '*' and every Kanbei-critical pattern"
