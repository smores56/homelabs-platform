# Public hosts whose clients cannot complete an Access login (mobile media
# apps), so a per-IP WAF rule is the only brute-force control. The zone plan
# allows exactly ONE http_ratelimit rule (a second fails the apply with "2 out
# of 1"), so the three hosts share one threshold, sized for the burstiest
# client: an Immich timeline scroll exceeds a few hundred requests per 10s. 1000
# is chosen for media; split a service out lower if the plan gains rule slots.
#
# The host list is the three media/reader applications in access.tf, not every
# public application: `recovery` is a static Pages deployment, not a proxied
# origin, and `agent-hooks` is a webhook receiver whose deliveries are
# individually authenticated and deduped. A shared per-IP budget would drop a
# legitimate burst of deliveries from one provider, and the source cannot be
# identified before the request is parsed, so the receiver imposes no limit and
# this rule does not cover it.
# Resource and ruleset names are historical; renaming forces a replacement,
# which the lifecycle block forbids.
resource "cloudflare_ruleset" "dav_rate_limit" {
  zone_id = data.cloudflare_zone.this.zone_id
  name    = "dav rate limit"
  kind    = "zone"
  phase   = "http_ratelimit"

  rules = [
    {
      action      = "block"
      description = "Rate limit the public app hosts per IP"
      expression  = "(http.host eq \"music.${var.domain}\" or http.host eq \"photos.${var.domain}\" or http.host eq \"opds.${var.domain}\")"
      ratelimit = {
        characteristics = ["ip.src", "cf.colo.id"]
        # Free plan allows only a 10-second period and mitigation window.
        period              = 10
        requests_per_period = 1000
        mitigation_timeout  = 10
      }
    },
  ]

  lifecycle {
    prevent_destroy = true
  }
}
