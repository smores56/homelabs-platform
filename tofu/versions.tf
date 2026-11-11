# Cloudflare zones, Access applications, routes and DNS records live here.
# Provider v5 only: cloudflare_zero_trust_access_application uses `destinations`,
# policies use `decision`, private routes use cloudflare_zero_trust_network_hostname_route.
# The token comes from CLOUDFLARE_API_TOKEN in the `cloudflare` environment; it is
# never a variable and never committed.

terraform {
  required_version = ">= 1.10.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}
