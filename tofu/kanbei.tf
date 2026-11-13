# Kanbei: a public hostname behind Cloudflare Access (SDLC #586).
#
# `kanbei.sammohr.dev` is published by the host tunnel: the ingress rule in
# nix-config (`webProxy.services.kanbei`) sends it to `https://127.0.0.1:443`,
# the relay into the guest, and the guest terminates TLS with its own
# certificate. The wildcard CNAME in `tofu/main.tf` resolves the name, so no
# record of its own is created. Access is the only gate.
#
# This replaced a private WARP hostname route. A private hostname needs Gateway's
# L4 rewrite and a client-side initial-resolved IP; a public hostname needs
# neither, and the same in-guest TLS still sits behind Access. The route below is
# retired in the follow-up change that deletes this resource: `prevent_destroy`
# is dropped here first, because OpenTofu refuses to destroy a resource whose
# state still carries the guard.

resource "cloudflare_zero_trust_network_hostname_route" "kanbei" {
  account_id = var.account_id
  hostname   = "kanbei.${var.domain}"
  tunnel_id  = var.host_tunnel_id
  comment    = "Kanbei chat: private hostname route (WARP only); TLS terminates in the guest"
}

# `allow_authenticate_via_warp` costs nothing here: a device that already runs
# the Cloudflare One client skips the browser login, and every other device gets
# the normal Access redirect. `skip_interstitial` keeps a JSON client off the
# confirmation page. Not in the app launcher.
resource "cloudflare_zero_trust_access_application" "kanbei" {
  account_id                  = var.account_id
  name                        = "kanbei"
  type                        = "self_hosted"
  session_duration            = "720h"
  allow_authenticate_via_warp = true
  skip_interstitial           = true
  app_launcher_visible        = false

  # A public destination: the browser reaches the edge, Access answers there,
  # and the tunnel ingress behind it maps the name to the guest's relay.
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
