# Access is default-deny at the edge. The wildcard application covers every
# one-label hostname under the zone and admits the owner's email and nothing
# else; the named applications below are the only overrides, and the public
# ones are the only places a bypass exists.
#
# Cloudflare evaluates a bypass policy before an allow policy and does not log
# bypassed requests, so a bypass application is a public endpoint by
# construction. Treat this list as the zone's public surface.

# Owner identity. Every protected application includes it, and `prevent_destroy`
# keeps one bad edit from deleting the last human way in.
resource "cloudflare_zero_trust_access_policy" "email" {
  account_id = var.account_id
  name       = "email allow-list"
  decision   = "allow"

  include = [{
    email = {
      email = var.email
    }
  }]

  lifecycle {
    prevent_destroy = true
  }
}

# Unattended clients: `cloudflared access ssh/tcp` and a blessed device's local
# proxy authenticate with an account service token instead of an IdP browser
# login. `non_identity`, so the request is authorized without an identity and
# the token secret never leaves the client.
resource "cloudflare_zero_trust_access_policy" "service_auth" {
  account_id = var.account_id
  name       = "service auth"
  decision   = "non_identity"

  include = [{
    any_valid_service_token = {}
  }]

  lifecycle {
    prevent_destroy = true
  }
}

# The only bypass policy. Attached to the public applications and never to the
# wildcard.
resource "cloudflare_zero_trust_access_policy" "bypass" {
  account_id = var.account_id
  name       = "public bypass"
  decision   = "bypass"

  include = [{
    everyone = {}
  }]

  lifecycle {
    prevent_destroy = true
  }
}

# The default-deny. One application, one allow policy, no bypass and no service
# auth: a hostname under the zone without its own application is reachable only
# by the owner's identity. A specific application wins for its exact hostname.
resource "cloudflare_zero_trust_access_application" "wildcard" {
  account_id       = var.account_id
  name             = "wildcard default-deny"
  type             = "self_hosted"
  session_duration = "720h"

  destinations = [{
    type = "public"
    uri  = "*.${var.domain}"
  }]

  policies = [{
    id         = cloudflare_zero_trust_access_policy.email.id
    precedence = 1
  }]

  lifecycle {
    prevent_destroy = true
  }
}

# Protected applications: the host's sshd, the memory MCP, and the assistant.
# Service auth where an unattended client reaches the endpoint, because a
# browser login is not available to `cloudflared access` or a device proxy.
resource "cloudflare_zero_trust_access_application" "ssh" {
  account_id       = var.account_id
  name             = "ssh"
  type             = "self_hosted"
  session_duration = "720h"

  destinations = [{
    type = "public"
    uri  = "ssh.${var.domain}"
  }]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.email.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.service_auth.id
      precedence = 2
    },
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_zero_trust_access_application" "memory" {
  account_id       = var.account_id
  name             = "memory"
  type             = "self_hosted"
  session_duration = "720h"

  destinations = [{
    type = "public"
    uri  = "memory.${var.domain}"
  }]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.email.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.service_auth.id
      precedence = 2
    },
  ]

  lifecycle {
    prevent_destroy = true
  }
}

# The assistant frontend. Access plus the app's own login is the second layer;
# the service-auth policy lets a blessed device's proxy in without the browser
# PIN page. The origin still runs on the host until the cluster cutover, so the
# name resolves through the wildcard to the host tunnel.
resource "cloudflare_zero_trust_access_application" "agent" {
  account_id       = var.account_id
  name             = "agent"
  type             = "self_hosted"
  session_duration = "720h"

  destinations = [{
    type = "public"
    uri  = "agent.${var.domain}"
  }]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.email.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.service_auth.id
      precedence = 2
    },
  ]

  lifecycle {
    prevent_destroy = true
  }
}

# `nanobot.sammohr.dev` is retired with the agent cutover (SDLC #583): the
# cluster serves the same app on `agent.sammohr.dev`, and the old application is
# gone rather than left as an email-only fallback. The block carried
# `prevent_destroy`, so its state entry is removed first and the object deleted
# at the edge (docs/manual-steps.md step 20).

locals {
  # Public hostnames: each one gets an explicit bypass application so the
  # wildcard's default stays deny rather than being widened for an app.
  #   recovery    - the data-recovery letter; its reader has no credentials by
  #                 definition, and it is static on Pages, not behind a tunnel.
  #   photos      - mobile clients cannot complete an Access login.
  #   music       - ditto, for the Navidrome clients.
  #   opds        - OPDS readers and the web reader; the app's own login is the
  #                 only gate, and it is rate limited (rate-limit.tf).
  #   agent-hooks - GitHub delivers webhooks with no browser and no service
  #                 token. The capability URL is the credential, and the
  #                 receiver's own HMAC check authenticates a source that has
  #                 one, so a bypass is the only workable decision here.
  #   ntfy        - the notification bus (SDLC #649). The Android app cannot
  #                 complete an Access login, and a service token cannot be
  #                 carried alongside the ntfy bearer the broker injects. The
  #                 app's own auth is the gate: deny-all default, no signup, and
  #                 ntfy's per-visitor request limiter.
  public_apps = toset([
    "recovery",
    "photos",
    "music",
    "opds",
    "agent-hooks",
    "ntfy",
  ])
}

resource "cloudflare_zero_trust_access_application" "public" {
  for_each = local.public_apps

  account_id = var.account_id
  name       = each.value
  type       = "self_hosted"

  destinations = [{
    type = "public"
    uri  = "${each.key}.${var.domain}"
  }]

  policies = [{
    id         = cloudflare_zero_trust_access_policy.bypass.id
    precedence = 1
  }]

  # No `prevent_destroy` here on purpose: this set must stay editable, and both
  # directions fail closed. Dropping a hostname deletes its bypass, so the
  # wildcard denies the name until someone reads the diff; adding one is a
  # reviewed widening of the public surface.
}
