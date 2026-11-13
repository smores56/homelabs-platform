#!/usr/bin/env bash
# OpenTofu against the Cloudflare zone, with the provider credential read from
# SOPS so no exports are needed:
#
#   ./scripts/tofu-cloudflare.sh plan
#   ./scripts/tofu-cloudflare.sh apply
#
# Every zone identifier comes from tofu/terraform.tfvars. Two credentials are
# environment-provided and this script is the only place that fetches them:
# the provider token (CLOUDFLARE_API_TOKEN) and the R2 pair for the state
# backend (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY). Override the encrypted
# file with CLOUDFLARE_SECRETS_FILE, or export them yourself to skip SOPS.
set -euo pipefail

root=$(git rev-parse --show-toplevel)
secrets_file=${CLOUDFLARE_SECRETS_FILE:-$HOME/repos/github.com/smores56/nix-config/secrets/smortress.yaml}

# The repo state is authoritative. A state directory left over from the old
# `just cloudflare-apply` flow leaves tofu with no state, which plans a
# whole-zone create that Cloudflare then rejects object by object (409 on every
# existing application and record). Refuse before that can happen.
legacy_state=${CLOUDFLARE_LEGACY_TOFU_DIR:-/var/lib/cloudflare-tofu}
if [[ -e $legacy_state && -z ${CLOUDFLARE_IGNORE_LEGACY_TOFU_DIR:-} ]]; then
  printf 'tofu-cloudflare: %s exists, but this repository state is authoritative.\n' "$legacy_state" >&2
  printf 'Remove it (sudo rm -rf %s) or set CLOUDFLARE_IGNORE_LEGACY_TOFU_DIR=1.\n' "$legacy_state" >&2
  exit 1
fi

if [[ -z ${CLOUDFLARE_API_TOKEN:-} ]]; then
  [[ -r $secrets_file ]] || {
    printf 'tofu-cloudflare: cannot read %s; set CLOUDFLARE_SECRETS_FILE\n' "$secrets_file" >&2
    exit 1
  }
  CLOUDFLARE_API_TOKEN=$(sops -d --extract '["cloudflare-api-token"]' "$secrets_file")
  export CLOUDFLARE_API_TOKEN
fi

# The state lives in R2 (tofu/backend.tf). Without the pair, `init` cannot reach
# it and every command below would fail with a confusing 403, so fetch it the
# same way as the token. The host's restic job uses the same key names.
if [[ -z ${AWS_ACCESS_KEY_ID:-} || -z ${AWS_SECRET_ACCESS_KEY:-} ]]; then
  [[ -r $secrets_file ]] || {
    printf 'tofu-cloudflare: cannot read %s; set CLOUDFLARE_SECRETS_FILE\n' "$secrets_file" >&2
    exit 1
  }
  AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-$(sops -d --extract '["r2-access-key-id"]' "$secrets_file")}
  AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-$(sops -d --extract '["r2-secret-access-key"]' "$secrets_file")}
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
fi
# R2 has no region; the backend and the SDK still want one.
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-auto}

exec tofu -chdir="$root/tofu" "$@"
