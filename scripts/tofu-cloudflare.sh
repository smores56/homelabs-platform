#!/usr/bin/env bash
# OpenTofu against the Cloudflare zone, with the provider credential read from
# SOPS so no exports are needed:
#
#   ./scripts/tofu-cloudflare.sh plan
#   ./scripts/tofu-cloudflare.sh apply
#
# Every zone identifier comes from tofu/terraform.tfvars. Only the credential is
# environment-provided, and this script is the only place that fetches it:
# override the encrypted file with CLOUDFLARE_SECRETS_FILE, or set
# CLOUDFLARE_API_TOKEN yourself to skip SOPS entirely.
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

exec tofu -chdir="$root/tofu" "$@"
