# Public hosts whose clients cannot complete an Access login (mobile media
# apps), so a per-IP WAF rule is the only brute-force control. The zone plan
# allows exactly ONE http_ratelimit rule (a second fails the apply with "2 out
# of 1"), so the three hosts share one threshold, sized for the burstiest
# client: an Immich timeline scroll exceeds a few hundred requests per 10s. 1000
# is chosen for media; split a service out lower if the plan gains rule slots.
#
# The host list matches the public applications in access.tf; `recovery` is not
# here because it is a static Pages deployment, not a proxied origin.
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
      expression  = "(http.host eq \"music.${var.domain}\" or http.host eq \"immich.${var.domain}\" or http.host eq \"calibre.${var.domain}\")"
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
