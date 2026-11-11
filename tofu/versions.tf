# Cloudflare zone, Access, tunnel routes and DNS for sammohr.dev.
#
# Moved here from nix-config's `tofu/` (SDLC #581). The resource addresses are
# the ones the operator's state already holds, so the move is a state-directory
# copy, not a re-import; renaming an address would need `tofu state rm` plus
# `import` (docs/cloudflare.md).
#
# Provider v5 names only; docs/cloudflare.md carries the v4 mapping:
#   cloudflare_dns_record                    (was cloudflare_record)
#   cloudflare_zero_trust_access_application (was cloudflare_access_application)
#   cloudflare_zero_trust_access_policy      (was cloudflare_access_policy)
#   destinations                             (was self_hosted_domains; supersedes `domain`)
#
# State is operator-owned and never in git: an apply runs from a checkout with
# its own state directory. No secret is an input: CLOUDFLARE_API_TOKEN comes
# from the environment (`cloudflare` Actions environment or a sops-decrypted
# file), and no variable carries a token.

terraform {
  required_version = ">= 1.10.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "cloudflare" {}
