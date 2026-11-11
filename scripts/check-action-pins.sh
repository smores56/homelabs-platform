#!/usr/bin/env bash
# Fails unless every remote action reference is pinned to a full commit SHA.
# An unpinned tag lets a compromised upstream retag a release and run code with
# whatever token the job holds.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

shopt -s nullglob
files=(.github/workflows/*.yml .github/workflows/*.yaml)
if ((${#files[@]} == 0)); then
  echo "no workflow files under .github/workflows"
  exit 0
fi

status=0
while IFS= read -r match; do
  file="${match%%:*}"
  rest="${match#*:}"
  lineno="${rest%%:*}"
  value="${rest#*:}"

  value="${value#*uses:}"
  [[ $value =~ ^[[:space:]]*([^[:space:]]+) ]] && value="${BASH_REMATCH[1]}"
  value="${value//\"/}"
  value="${value//\'/}"

  case "$value" in
    ./*|docker://*) continue ;;
  esac

  ref="${value##*@}"
  if [[ ! $ref =~ ^[0-9a-f]{40}$ ]]; then
    echo "$file:$lineno: action is not pinned to a full SHA: $value" >&2
    status=1
  fi
done < <(grep -rEn '^[[:space:]]*(-[[:space:]]*)?uses:[[:space:]]*[^[:space:]]+' \
  "${files[@]}")

if ((status != 0)); then
  echo "pin every action to a 40-character commit SHA" >&2
  exit 1
fi

echo "all action references are SHA-pinned"
