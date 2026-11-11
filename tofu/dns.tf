# Cluster tunnel routes. A specific record wins over *.<domain>, so a name in
# cluster_hosts resolves to the cluster connector while everything else keeps
# resolving to the host tunnel in main.tf. The set is empty until a cutover, so
# the split is a variable change and a rollback is deleting the entry.
resource "cloudflare_dns_record" "cluster" {
  for_each = toset(var.cluster_hosts)

  zone_id = data.cloudflare_zone.this.zone_id
  name    = "${each.value}.${var.domain}"
  type    = "CNAME"
  content = local.cluster_tunnel_cname
  proxied = true
  ttl     = 1
  comment = "cluster tunnel (k3s ingress/cloudflared)"

  lifecycle {
    precondition {
      condition     = var.cluster_tunnel_id != ""
      error_message = "cluster_hosts is non-empty but cluster_tunnel_id is empty: mint the cluster tunnel first (docs/manual-steps.md)."
    }
  }
}

# CAA pins issuance for the zone to the ACME account Kanbei's in-VM client uses
# (docs/cloudflare.md, "CAA"). Two tags because a wildcard order is authorized
# by issuewild, not issue.
#
# Caveat that makes this a narrowing, not a lockdown: Cloudflare appends its own
# Universal SSL CAA records (pki.goog, letsencrypt.org, ssl.com, sectigo.com,
# digicert) to any zone that has a CAA record. Those are invisible in the
# dashboard and appear in `dig`, so an unpinned `letsencrypt.org` issue always
# sits beside the pinned one and any Let's Encrypt account stays authorized for
# the edge certificate. Verify with:
#   dig +short CAA sammohr.dev
resource "cloudflare_dns_record" "caa_issue" {
  zone_id = data.cloudflare_zone.this.zone_id
  name    = var.domain
  type    = "CAA"
  ttl     = 3600
  comment = "ACME account pin for the in-VM CSR client"

  data = {
    flags = 0
    tag   = "issue"
    value = "letsencrypt.org; accounturi=${local.acme_account_uri}"
  }
}

resource "cloudflare_dns_record" "caa_issuewild" {
  zone_id = data.cloudflare_zone.this.zone_id
  name    = var.domain
  type    = "CAA"
  ttl     = 3600
  comment = "ACME account pin for wildcard orders from the in-VM CSR client"

  data = {
    flags = 0
    tag   = "issuewild"
    value = "letsencrypt.org; accounturi=${local.acme_account_uri}"
  }
}
