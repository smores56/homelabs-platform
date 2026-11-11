# The recovery page is static on Cloudflare Pages. GitHub Actions builds `recovery/dist`
# with Deno and uploads it with wrangler; nothing here holds the content itself.
#
# The wildcard record above sends every other name to the tunnel, so the page needs its own
# record: a specific name wins over `*.sammohr.dev`, and without it the letter's URL would
# reach smortress and 404.

# The project name is the public default host (<name>.pages.dev). Renaming it replaces the
# project: Cloudflare rebuilds it empty and the domain binding below moves over.
resource "cloudflare_pages_project" "recovery" {
  account_id        = var.account_id
  name              = "sam-mohr-data-recovery"
  production_branch = "main"
}

# The provider does not create the DNS record for a Pages domain; the record below is
# part of the same change.
resource "cloudflare_pages_domain" "recovery" {
  account_id   = var.account_id
  project_name = cloudflare_pages_project.recovery.name
  name         = "recovery.${var.domain}"
}

resource "cloudflare_dns_record" "recovery" {
  # The project subdomain follows the project name above; keep the two in step.
  zone_id = data.cloudflare_zone.this.zone_id
  name    = "recovery.${var.domain}"
  type    = "CNAME"
  content = "sam-mohr-data-recovery.pages.dev"
  proxied = true
  ttl     = 1

  depends_on = [cloudflare_pages_domain.recovery]
}
