#!/usr/bin/env bash
# Static assertion for the fork-PR secret boundary (SDLC #576, relaxed by #622).
#
# Fork pull requests run with a read-only GITHUB_TOKEN and no other secrets, so
# a `secrets.*` reference inside a `pull_request`-triggered workflow is dead
# code at best and a latent exfiltration path at worst. One file is exempt:
# `tofu-plan-pr.yml` makes the merge an informed approval by planning on the
# PR, and it may hold only the read-only pair. Every other workflow keeps the
# blanket ban, and the exemption is checked structurally here rather than
# trusted: the guards below are what make the exemption safe.
#
# `pull_request_target` stays forbidden outright. It also stops working by
# default in public repositories on 2026-12-02 (GitHub changelog), so nothing
# may be built on it.
#
# The runtime half of the test needs a second GitHub identity;
# docs/negative-tests.md records it as manual.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

exempt=.github/workflows/tofu-plan-pr.yml
readonly -a allowed=(CLOUDFLARE_PLAN_API_TOKEN R2_STATE_READONLY_ACCESS_KEY_ID R2_STATE_READONLY_SECRET_ACCESS_KEY)

shopt -s nullglob
files=(.github/workflows/*.yml .github/workflows/*.yaml)
if ((${#files[@]} == 0)); then
  echo "no workflow files under .github/workflows"
  exit 0
fi

status=0
allowed_secret() {
  local want=$1 candidate
  for candidate in "${allowed[@]}"; do
    [[ $candidate == "$want" ]] && return 0
  done
  return 1
}

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
    if [[ $file == "$exempt" ]] && allowed_secret "$secret"; then
      continue
    fi
    echo "$file: pull_request workflow references secrets.$secret" >&2
    status=1
  done < <(grep -oE 'secrets\.[A-Za-z0-9_]+' "$file" | sed 's/^secrets\.//' | sort -u)
done

# The exemption is only as strong as its guards. Each guard carries a marker
# comment, and the marker is required text, so deleting a guard fails CI on the
# same diff. tests/tofu/check.sh asserts the guard bodies themselves.
if [[ -f $exempt ]]; then
  require() {
    if ! grep -qE "^[[:space:]]*# guard: $1$" "$exempt"; then
      echo "$exempt: missing guard '$1'" >&2
      status=1
    fi
  }
  require "fork-only"
  require "ci-change-refused"
  require "exec-label"
  require "read-only-credentials"
  if grep -qE 'secrets\.(CLOUDFLARE_API_TOKEN|R2_STATE_ACCESS_KEY_ID|R2_STATE_SECRET_ACCESS_KEY)\b' "$exempt"; then
    echo "$exempt: the pre-merge plan may hold only the read-only credentials" >&2
    status=1
  fi
fi

if ((status != 0)); then
  echo "fork PRs must not reach any secret except the read-only GITHUB_TOKEN and the read-only tofu plan pair" >&2
  exit 1
fi

echo "no pull_request workflow references secrets beyond GITHUB_TOKEN and the read-only plan pair"
