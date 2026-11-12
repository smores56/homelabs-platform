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

if [[ -z ${CLOUDFLARE_API_TOKEN:-} ]]; then
  [[ -r $secrets_file ]] || {
    printf 'tofu-cloudflare: cannot read %s; set CLOUDFLARE_SECRETS_FILE\n' "$secrets_file" >&2
    exit 1
  }
  CLOUDFLARE_API_TOKEN=$(sops -d --extract '["cloudflare-api-token"]' "$secrets_file")
  export CLOUDFLARE_API_TOKEN
fi

exec tofu -chdir="$root/tofu" "$@"
