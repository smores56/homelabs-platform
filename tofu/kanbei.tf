# Kanbei: a private hostname route on the human-owned host tunnel (SDLC #586).
#
# `kanbei.sammohr.dev` is reachable only from owner devices running the
# Cloudflare One (WARP) client. The route is Layer 4: Gateway resolves the name
# through the tunnel and forwards the TCP stream unchanged, so TLS terminates in
# the guest (docs/kanbei-access.md in nix-config). Deliberately:
#
#   - no `cloudflare_dns_record`: a specific record would make the name a public
#     hostname. `*.sammohr.dev` still resolves it to the host tunnel, but the
#     tunnel has no ingress rule, so a non-WARP request reaches the 404 fallback
#     and Access denies it first;
#   - no tunnel ingress rule: the host tunnel's ingress stays in nix-config and
#     is what keeps a protected name from gaining an origin path;
#   - Access still gates the route, but with the owner email only. The account
#     `service_auth` policy is deliberately NOT attached: it accepts *any* valid
#     service token in the account, including the agent's, which would let a
#     machine identity open Kanbei behind the owner's back.
#
# The fragment is the one in nix-config `docs/kanbei-access.md`, adapted to the
# provider v5 `destinations` form used by the rest of access.tf. No token is
# created here: an unattended client needs its own reviewed policy (and ideally
# its own application), not the account-wide service-auth decision; see
# docs/manual-steps.md, "Kanbei private route and service token".
resource "cloudflare_zero_trust_network_hostname_route" "kanbei" {
  account_id = var.account_id
  hostname   = "kanbei.${var.domain}"
  tunnel_id  = var.host_tunnel_id
  comment    = "Kanbei chat: private hostname route (WARP only); TLS terminates in the guest"

  lifecycle {
    prevent_destroy = true
  }
}

# `allow_authenticate_via_warp` lets an enrolled device pass with its WARP
# identity instead of a browser redirect; `skip_interstitial` keeps a JSON
# client from landing on the Access confirmation page. The app is not in the app
# launcher because the route only answers from inside the tunnel.
resource "cloudflare_zero_trust_access_application" "kanbei" {
  account_id                  = var.account_id
  name                        = "kanbei"
  type                        = "self_hosted"
  session_duration            = "720h"
  allow_authenticate_via_warp = true
  skip_interstitial           = true
  app_launcher_visible        = false

  # `public` is the destination type every other self-hosted app here uses; the
  # name is private because the route has no DNS record and no ingress rule, not
  # because Access sees a different destination kind.
  destinations = [{
    type = "public"
    uri  = "kanbei.${var.domain}"
  }]

  # Exactly one policy. `tests/tofu/check.sh` asserts the count, so adding the
  # account-wide service-auth decision back here (or a bypass) fails CI.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.email.id
      precedence = 1
    },
  ]

  lifecycle {
    prevent_destroy = true
  }
}
