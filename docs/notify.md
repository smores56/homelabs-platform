# The notification bus

ntfy in the `notify` namespace is the one place a notification is published.
The phone, the laptop, host systemd units, Alertmanager, Flux and the agent all
write to it; nothing sends mail.

## Shape

| Piece | Where |
| --- | --- |
| `ntfy` Deployment, Service, claim, config | `clusters/homelab/notify/` |
| Users, tokens, ACL | `ntfy-auth` Secret, projected by ESO from `ntfy-auth-sops` |
| Public hostname | `ntfy.sammohr.dev`, cluster tunnel, Access bypass (`docs/cloudflare.md`) |
| Cluster alerts | `notifyrelay` in `observability` -> `ntfy.notify:8080` (Alertmanager `/alert`, Flux `/flux`) |
| Host units | `ntfy-publish@.service` in `nix-config` -> `https://ntfy.sammohr.dev/alerts` |
| The agent | `$BROKER_URL/notify/agent` |

## Topics and identity

| Topic | Publisher | Subscriber |
| --- | --- | --- |
| `alerts` | `publisher` (host, relay) | `reader` (phone, laptop) |
| `agent` | `agent` (the broker) | `reader` |

`sam` is the admin and the web UI login; admin is read-write on everything, so
it needs no ACL entry and ntfy rejects one. ACL is per user and a token
authenticates as its user, so a `publisher` token cannot subscribe and a
`reader` token cannot publish.

The broker gets `agent`, not `publisher`, because its `allow_paths` is a
single-segment regex rather than a topic allowlist: with `agent:agent:wo` the
bus enforces the boundary, not the broker.

## Why the broker takes the public path

The `notify` enrollment keeps `allow_private: false`, so the broker resolves
`ntfy.sammohr.dev` to Cloudflare edge addresses and the request leaves through
the tunnel and comes back. An in-cluster target would need
`allow_private: true`, which makes the broker a path into the cluster network --
the one thing the enrollment model exists to prevent. The relay has no such
rule to honour, so it uses the Service directly and cluster alerts do not depend
on the tunnel they may be reporting on.

## Delivery on the phone

A self-hosted server gets no FCM: ntfy uses Firebase only for `ntfy.sh`, and
`firebase-key-file` alone is not enough -- it needs a personal Firebase project
and a custom-built APK. Instant delivery is therefore a foreground service
holding a WebSocket, which survives doze and the app being swiped away, at the
cost of a permanent low-priority notification and a battery-optimisation
exemption. The F-Droid flavor always runs this way and has no toggle.

Web Push through the installed PWA is the second channel: server-side VAPID
keys, no Firebase account, and it works with the app and the browser closed. On
Android the browser's push endpoint is `fcm.googleapis.com` -- Google operates
the transport, but no credential of ours is involved.

## Failure

There is no out-of-band channel once the mail path is gone. The two Cloudflare
tunnel-health policies stay on email for exactly that reason: they detect the
tunnel the bus is reached through. `ntfy-publish@` failures land in the journal,
and `NtfyDown` fires when Prometheus stops scraping `/metrics`.

Cluster alerts do not depend on the tunnel: `notifyrelay` posts to the Service,
not to `ntfy.sammohr.dev`, so a tunnel outage still delivers Prometheus and Flux
failures. The agent's path does depend on it — the broker resolves the public
hostname — so agent notifications fail while the tunnel is down and the chat
channel is unaffected.
