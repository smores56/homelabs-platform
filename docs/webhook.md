# Inbound webhooks

SDLC #627. GitHub (and any other provider) delivers events to
`agent-hooks.sammohr.dev`, which terminates on the cluster tunnel, lands on the
`webhook` Deployment in the `webhook` namespace, and is enqueued into the agent
through nanobot's WebSocket gateway. The agent still has no inbound listener of
its own and no public egress.

```text
provider --HTTPS--> agent-hooks.sammohr.dev (Access bypass)
          --> cluster tunnel (ingress/cloudflared)
          --> webhook.webhook.svc:8080            capability id, HMAC, 1 MiB, dedupe
          --> ws://nanobot.tenant-apps.svc:8765   one enqueue frame
          --> the agent's session
```

Source: `apps/webhook/` (Python stdlib, 78 unit tests). Manifests:
`clusters/homelab/webhook/`. Applied by the Flux stage `platform-webhook`.

## Trust boundary and non-goals

The receiver is **unauthenticated by construction**. It has no session, no
cookie and no Access policy: GitHub cannot complete an Access login and holds
no service token, so the only workable edge decision is a bypass. What stands
between the internet and an agent turn is:

1. a **capability URL**: a 256-bit, URL-safe id that is the path segment
   (`/hooks/<id>`). It is compared by exact dictionary lookup, so a guess is a
   404. The id is never logged.
2. an optional **GitHub HMAC**: a source registered with a secret requires
   `X-Hub-Signature-256` to verify over the exact body. A source registered
   without one accepts unsigned deliveries; the capability id is then the only
   credential.
3. a **size limit** and **dedupe**, so one hook cannot be used to pump the
   agent or to replay a redelivery.
4. an **admin token** on registration and revocation. Without it, anyone who
   found the public hostname could mint their own capability and enqueue into
   the agent. The registration route is the reason the admin secret exists.

Not in scope: it is not a general event bus, it stores nothing but source
records, and it does not call any provider API. Its only outbound request is
the enqueue path below.

## Request contract

| Request | Auth | Result |
| --- | --- | --- |
| `GET /healthz` | none | `200 {"status":"ok"}`; also the kubelet probes |
| `POST /endpoints` | `Authorization: Bearer <admin-token>` | `201` with `name`, `capability_id`, `capability_url`, `hmac` |
| `DELETE /endpoints/<capability-id>` | admin token | `204`, or `404` for an unknown id |
| `POST /hooks/<capability-id>` | capability id + HMAC when the source has a secret | `202 accepted`; `200 duplicate`; `401` bad/missing signature; `404` unknown id; `413` oversized; `502` gateway unavailable |

Registration body: `{"name": "github-platform", "secret": "<github webhook
secret>"}`. `secret` is optional. The name is 1–64 lowercase alphanumerics and
hyphens; it is the unit of rotation.

- **Rotation.** Registering an existing name issues a new id and deletes the old
  one in the same write, so the old URL 404s immediately. Rotate both the id and
  the HMAC secret with one call; update the provider's webhook URL to the new
  `capability_url`.
- **Revocation.** `DELETE /endpoints/<id>` removes the source (and its secret).
- **Size.** A body over **1 MiB** (`MAX_BODY_BYTES`) is refused with `413` before
  it is read. A missing `Content-Length` is `411`; a chunked body is not
  accepted. The text forwarded to the agent is capped at **64 KiB**
  (`MAX_FORWARD_BYTES`) and marked `[body truncated …]`; the full body is not
  retained anywhere.
- **Dedupe.** The key is `<source-name>:<delivery-id>`, where the delivery id is
  `X-GitHub-Delivery`, else `X-Delivery-Id`/`X-Request-Id`, else the body's
  SHA-256. A repeat within the window is `200 duplicate` and is not enqueued. The
  cache is bounded (4096 ids, 6 h) and in memory: a restart forgets the window,
  which is why a provider retry that crosses a rollout can enqueue twice.
- **Failure.** If the gateway cannot be reached, the delivery gets `502`, the
  dedupe claim is released, and the provider's retry is accepted normally.

Logging is one line per request: method, route (`hook`, `endpoints`, …), status
and byte count. No header value, body, capability id or secret is logged.

## Persistence

Sources live on the `webhook-sources` PVC (`local-path`, 64 MiB, RWO):
`sources.json`, mode `0600`, written atomically. The claim holds the per-source
HMAC secrets, so it is why the pod has one replica (`Recreate`) and why no other
namespace can mount it. A PVC was chosen over memory so a capability URL
survives a rollout; the dedupe cache deliberately does not.

The tokens are separate, operator-created Secret keys (`webhook-tokens`):

| Key | Used for | Source |
| --- | --- | --- |
| `admin-token` | `POST`/`DELETE /endpoints` | `openssl rand -hex 32` |
| `enqueue-token` | `/webui/bootstrap` bearer | nanobot's `channels.websocket.tokenIssueSecret` |

The Deployment mounts the Secret with `optional: false`, so a missing Secret
leaves the pod pending instead of serving a public hostname with no tokens.

## Enqueue: the researched gateway protocol

The client is `apps/webhook/webhook/gateway.py` plus a minimal RFC 6455 client
in `ws.py`. The contract was read from the installed `nanobot-ai==0.4.1` package
(`nanobot/channels/websocket/runtime.py`, `nanobot/webui/ws_http.py`,
`nanobot/webui/inbound_commands.py`), which is what the `nix-config` image
runs:

1. `GET /webui/bootstrap` with `Authorization: Bearer <enqueue-token>` returns
   `{"token": <one-time>, "ws_url": "ws://…:8765/"}`.
2. Connect to `ws_url?token=<one-time>&client_id=webhook`; the gateway sends
   `{"event":"ready","chat_id":"…"}`.
3. Send exactly one frame and read the acknowledgement:

   ```json
   {"type": "message", "chat_id": "<ready chat_id>", "content": "<text>",
    "webui": true, "turn_id": "<delivery id>"}
   ```

   `webui: true` is what makes the gateway answer `{"event":"message_accepted"}`;
   an `{"event":"error"}` frame fails the enqueue with its `detail`.

No other verb is implemented: no chat creation, no settings call, no
attachment upload, no second message. Each delivery gets a fresh connection and
therefore a fresh session, so the agent sees one turn per webhook rather than
continuity into a previous one.

### Integration risks

- **The token is generated, not injected.** The image's `docker_bootstrap`
  writes `channels.websocket.tokenIssueSecret` from `NANOBOT_WEB_TOKEN`, or a
  random value when that variable is absent. `homelabs-apps` does not set it, so
  today the operator reads the value from the pod's rendered config
  (`$HOME/.nanobot/config.json`). If the scratch PVC is ever reset, the gateway
  mints a new secret and every enqueue 502s until `enqueue-token` is updated.
  The durable fix is a tenant-side `NANOBOT_WEB_TOKEN` from a SOPS/ESO secret;
  that is a `homelabs-apps` change, not this repository's.
- **The envelope is a WebUI protocol, not a public API.** nanobot's
  `webui/client_contract.py` versions it (`WEBUI_PROTOCOL = 1`), but a future
  release can change the frame. The unit tests pin the exact frames against a
  fake gateway, so a bump that breaks them fails the platform suite rather than
  production.
- **The WebSocket listener must stay enabled.** `channels.websocket.enabled`
  defaults to true and the image's bootstrap writes it; a tenant config that
  disables it removes the only enqueue path.
- **Delivery is at-least-once.** The receiver answers `202` only after the
  gateway accepted the frame, but a crash between acceptance and the response
  makes the provider retry; the in-memory dedupe cannot survive that restart.

## Routing

`tofu/cluster.tf` maps the hostname to the Service and `tofu/dns.tf` follows the
same map:

```hcl
agent-hooks = "http://webhook.webhook.svc.cluster.local:8080"
```

`tofu/access.tf` adds `agent-hooks` to `local.public_apps`, the explicit bypass
set: Cloudflare evaluates a bypass before an allow policy and does not log
bypassed requests, so that list *is* the zone's public surface. The zone allows
exactly one rate-limit rule and it does **not** cover this host: a shared
per-IP budget would drop a legitimate burst of deliveries, and the receiver has
no rate limit of its own — the capability URL, HMAC, size limit and dedupe are
the controls.

NetworkPolicy is default-deny in both directions. The receiver admits only
`ingress/cloudflared` on 8080 and reaches only `tenant-apps/nanobot` on 8765
(plus DNS). It has no public egress, so a delivery cannot be spilled anywhere
else. The remote halves live in `clusters/homelab/platform/network/webhook-flows.yaml`.

## Operations

```sh
# unit tests (no cluster, no network)
nix shell nixpkgs#python312 -c python3 -m unittest discover -s apps/webhook/tests -t apps/webhook -v

# the manifests and the policy contract
nix shell nixpkgs#kustomize -c kustomize build clusters/homelab/webhook
nix shell nixpkgs#yq-go nixpkgs#jq nixpkgs#kustomize -c tests/policies/check.sh

# static Cloudflare shape (the bypass app and the tunnel route)
nix shell nixpkgs#opentofu -c tests/tofu/check.sh
```

Live-gated (need the cluster, a provider and the operator):

- publishing the image and replacing the all-zero digest placeholder;
- creating `webhook-tokens` and registering the first source
  (`docs/manual-steps.md`, "Publish the webhook image and create its tokens");
- the first real GitHub delivery and the agent's reply;
- NetworkPolicy and gVisor enforcement (no CNI/kubelet on the test bench).
