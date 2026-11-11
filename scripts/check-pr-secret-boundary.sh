#!/usr/bin/env bash
# Static assertion for the fork-PR secret boundary (SDLC #576).
#
# Fork pull requests run with a read-only GITHUB_TOKEN and no other secrets, so
# a `secrets.*` reference inside a `pull_request`-triggered workflow is either
# dead code or a latent exfiltration path, and `pull_request_target` runs with
# the base repository's secrets. Both fail here. The runtime half of the test
# needs a second GitHub identity; docs/negative-tests.md records it as manual.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

shopt -s nullglob
files=(.github/workflows/*.yml .github/workflows/*.yaml)
if ((${#files[@]} == 0)); then
  echo "no workflow files under .github/workflows"
  exit 0
fi

status=0
for file in "${files[@]}"; do
  if grep -qE '^[[:space:]]*pull_request_target[[:space:]]*:' "$file"; then
    echo "$file: pull_request_target runs with base-repo secrets; forbidden" >&2
    status=1
  fi

  grep -qE '^[[:space:]]*pull_request(_[a-z]+)?[[:space:]]*:' "$file" || continue

  while IFS= read -r secret; do
    case "$secret" in
      GITHUB_TOKEN|github_token) continue ;;
    esac
    echo "$file: pull_request workflow references secrets.$secret" >&2
    status=1
  done < <(grep -oE 'secrets\.[A-Za-z0-9_]+' "$file" | sed 's/^secrets\.//' | sort -u)
done

if ((status != 0)); then
  echo "fork PRs must not reach any secret except the read-only GITHUB_TOKEN" >&2
  exit 1
fi

echo "no pull_request workflow references secrets beyond GITHUB_TOKEN"
