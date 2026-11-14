# The agent broker

SDLC #582. nanobot has no credentials, no cluster token and no direct egress.
Every outbound request leaves through one broker Deployment in the `broker`
namespace; the broker maps an enrolled **logical host** to one real endpoint,
resolves and pins the address itself, strips whatever credential the agent sent,
injects exactly one enrolled credential, refuses redirects that leave the
enrolled endpoint, and logs metadata only.

Source: `apps/broker/` (Python stdlib, 102 unit tests). Manifests:
`clusters/homelab/broker/`. Applied by the Flux stage `platform-broker`.

## Trust boundary and non-goals

The broker is inside the boundary: it holds the provider and GitHub tokens and
is the only place they exist in the cluster. It is **not** an egress proxy and
**not** a policy engine: there is no user identity, no per-tool authorization
and no action model. It enforces two reviewed layers before a credential is
attached — target integrity (the agent names a logical host and a relative
path, never a hostname, IP, SNI or `Host`) and the per-host request predicates
below (SDLC #624). A predicate is a narrow, config-reviewed gate on one
enrollment; everything a predicate does not forbid is limited only by
provider-side authorization, so an enrolled token remains a credential oracle
within its scope.

`nanobot` holds no credential, including no broker token. Caller authentication
is the namespace boundary: `NetworkPolicy` admits only tenant-tier pods to the
Service, and the agent's own `Authorization`, `Cookie`, `X-Api-Key`,
`Proxy-Authorization`, `X-Scope-OrgID` and `X-Forwarded-For` headers are
dropped before injection. There is no shared secret to leak, rotate or mount.

## Request contract

```text
<method> http://broker.broker.svc.cluster.local:8080/<logical-host>/<path>?<query>
```

- The first path segment is the logical host; the rest is the upstream path.
  `/tinyfish` and `/tinyfish/` both mean "/" upstream.
- `Host` must be a DNS name, not an IP literal; absolute-form targets
  (`GET http://host/...`), `//`-ambiguous paths, NUL and backslash bytes are
  refused with 400.
- `CONNECT` and `TRACE` are refused with 405. The broker is not a forward
  proxy, so there is no tunnel and no `HTTPS_PROXY` setting in the agent.
- Every other method is forwarded when the destination passes the checks.
- A request that fails its logical host's predicates is refused with `403` (or
  `413` for a body over that host's cap) before DNS resolution; nothing is
  forwarded. See "Per-host request predicates".
- The request body is read up to 64 MiB, or less when the logical host sets
  `max_body_bytes`; a chunked request body is refused (411) rather than
  half-forwarded.
- `/healthz` is the kubelet probe and never proxied.

The agent's environment points at this Service:

```text
BROKER_URL=http://broker.broker.svc.cluster.local:8080
TINYFISH_BASE_URL=http://broker.broker.svc.cluster.local:8080/tinyfish
```

## Logical-host enrollment

`clusters/homelab/broker/config/broker.json` is the reviewed, non-secret
enrollment. Static credentials are files in the `broker-credentials` Secret;
GitHub App enrollments name the app id, private key and installation id files
instead, and the broker mints a short-lived, scoped installation token for
each request's logical host (see "GitHub App identity" below).

| Logical host | Real endpoint | Injection | Credential | Private allowed |
| --- | --- | --- | --- | --- |
| `neuralwatt` | `https://api.neuralwatt.com:443` | `Authorization: Bearer` | `neuralwatt-api-key` | no |
| `github-apps` | `https://github.com:443` | `Authorization: Basic x-access-token:<token>` | app: `homelabs-apps`, `contents: write` | no |
| `github-platform-push` | `https://github.com:443` | `Authorization: Basic x-access-token:<token>` | app: `homelabs-platform`, `contents: write` | no |
| `github-platform` | `https://api.github.com:443` | `Authorization: Bearer` | app: `homelabs-platform`, `contents: write`, `pull_requests: write` | no |
| `github-agent-push` | `https://github.com:443` | `Authorization: Basic x-access-token:<token>` | app: `nix-config`, `kanbei`, `contents: write`, `workflows: write` | no |
| `github-agent` | `https://api.github.com:443` | `Authorization: Bearer` | app: `nix-config`, `kanbei`, `contents: read`, `pull_requests: write` | no |
| `github-sdlc-git` | `https://github.com:443` | `Authorization: Basic x-access-token:<token>` | app: `sdlc`, `contents: write` | no |
| `github-sdlc` | `https://api.github.com:443` | `Authorization: Bearer` | app: `sdlc`, `issues: write`, `metadata: read` | no |
| `github-hooks` | `https://api.github.com:443` | `Authorization: Bearer` | app: `sdlc`, `homelabs-apps`, `repository_hooks: write`, `metadata: read` | no |
| `tinyfish` | `https://api.search.tinyfish.ai:443` | `X-Api-Key: <token>` | `tinyfish-api-key` | no |
| `loki` | `http://loki-gateway.observability.svc.cluster.local:3100` | `Authorization: Bearer`, fixed `X-Scope-OrgID: data` | `loki-gateway-token` | yes |
| `memory` | `http://memory-mcp.graphiti.svc.cluster.local:8182` | `Authorization: Bearer` | `memory-mcp-token` (optional) | yes |
| `notify` | `https://ntfy.sammohr.dev:443` | `Authorization: Bearer` | `notify-token` (optional) | no |
| `self` | `http://agent-control.tenant-apps.svc.cluster.local:8081` (prefix `/self`) | none | — | yes |
| `cluster` | `http://agent-control.tenant-apps.svc.cluster.local:8081` (prefix `/cluster`) | none | — | yes |
| `status` | `http://status-exporter.observability.svc.cluster.local:9101` | none (metrics text) | — | yes |

`path_prefix` restores a service's own route namespace after the broker
strips the logical host: the caller addresses `BROKER_URL/self/status`, and
the service sees `/self/status`; `BROKER_URL/cluster/pods` becomes
`/cluster/pods`. Without it the upstream would only see the trailing path.

`github-platform-push` is the git transport half of the `github-platform`
capability: GitHub serves git smart HTTP only on `github.com`, while PR calls
are REST on `api.github.com`. The agent pushes `agent/*` branches to
`homelabs-platform` and opens same-repo pull requests; there is no fork (a
GitHub App cannot fork, and GitHub refuses a fork of your own private
repository), so branch rulesets, not the fork boundary, keep the identity off
every other branch (docs/manual-steps.md step 1).

`github-platform` asks for `contents: write` because merging a pull request is a
contents write in the REST API; `pull_requests: write` alone is not enough and
the merge call fails with `403 Resource not accessible by integration`. The
scope is still bounded by the rulesets, not by this file: `agent-branch-gate`
allows create/update only on `refs/heads/agent/**`, and `main` requires a pull
request with an approving review, so the token can merge what a human approved
and nothing else.

`github-agent-push` and `github-agent` are the same pair for the agent's own
repositories: `nix-config` and `kanbei`. They exist so the agent can propose
changes there the way it does for `homelabs-platform` — push an `agent/*`
branch, open a pull request — without a second credential. The repositories are
listed on both hosts and the installation token is minted per request, so the
scope is exactly those two repos. Write access is still governed per repository
by the `agent-gating` and `main` rulesets, not by this file: the token can only
reach `agent/*`, and `main` requires a reviewed pull request. Adding a
repository here is therefore not enough on its own — the rulesets must already
be in place.

`github-agent-push` also asks for `workflows: write`. GitHub refuses a push that
touches `.github/workflows/` without it, and it refuses the whole push, not just
the workflow file, so without the permission the agent cannot land a CI change it
proposed. It is not a wider reach into the repositories: the same rulesets bound
it, and a workflow change still needs a reviewed pull request on `main`.

`github-sdlc-git` and `github-sdlc` (SDLC #629) are that pair for the personal
work registry `smores56/sdlc`: git transport on `github.com`, REST on
`api.github.com`, both scoped to that one repository so the agent can clone,
push an `agent/*` branch and open the registry PR. The git half holds
`contents: write`; the API half holds `issues: write` and `metadata: read`,
because the registry edits issues and nothing else, and GitHub requires
`metadata: read` on every installation token. `sdlc` must be in the app
installation before these hosts work (docs/manual-steps.md step 1, item 2): a
repository named here but absent from the installation fails the whole host with
a 422.

`github-hooks` (SDLC #628) is the outbound half of the webhook receiver: the
agent registers repository hooks that GitHub delivers to
`https://agent-hooks.sammohr.dev/`, so this host only ever creates one. The
enrollment holds `repository_hooks: write` plus `metadata: read` on `sdlc` and
`homelabs-apps`, admits only `POST /repos/<owner>/<repo>/hooks`, and a body
guard requires `config.url` to start with `https://agent-hooks.sammohr.dev/`.
The narrow shape is the point: `PATCH`/`DELETE` (`method_denied`) would retarget
or delete a hook the human installed, and without the body guard the agent could
point a fresh hook at an attacker URL, whose deliveries carry the repository's
event payloads. The App needs the **Repository webhooks: read and write**
permission and a re-consented installation first (docs/manual-steps.md step 34):
an installation token cannot grow a permission the installation never approved,
so hook creation fails with `403`/`422` until then.

`memory` (SDLC #631) is the graphiti memory wrapper
(`memory-mcp.graphiti.svc.cluster.local:8182`), the same MCP endpoint
`memory.sammohr.dev` fronts, reached in-cluster instead of through the tunnel.
It is plain HTTP with `allow_private: true` — the documented private exception —
and injects `Authorization: Bearer <memory-mcp-token>`. The value is the one the
wrapper already uses, projected from the graphiti Secret into
`broker-credentials` by docs/manual-steps.md step 32. The enrollment sets
`optional_secret`, so until that file exists the host answers
`credential_unavailable` (503) and the rest of the broker is unaffected; a
request is never forwarded without the credential.

Every repository named on a host goes into a single token request, so one name
that is not in the app installation fails the whole host with a 422. Add a
repository only after confirming it is in the installation.

`notify` (SDLC #640, #649) is the ntfy bus at `https://ntfy.sammohr.dev`. The
path is the topic, the message is the plain-text body, and `title`, `priority`
and `tags` ride in the query string — the broker matches `allow_paths` against
the path only, so the query is forwarded untouched. It admits `POST` on a
single-segment topic of at most 64 characters, caps the body at 4 KiB and
injects `Authorization: Bearer <notify-token>`. `allow_private` stays `false`:
the broker resolves the hostname to Cloudflare edge addresses, so the request
leaves through the tunnel and comes back, and the broker never becomes a path
into the cluster network.

The token is the ntfy `agent` user's, which is write-only on the `agent` topic.
That is deliberate: `allow_paths` is a path regex, not a topic allowlist, so a
`publisher` token would let the agent page `alerts`. The bus enforces the
boundary, not the broker. The enrollment sets `optional_secret`, so an absent
`notify-token` denies that host with `credential_unavailable` (503) instead of
aborting the broker at startup — the broker also serves GitHub, SDLC, memory
and Loki, and a missing notification token must not take those down. A request
is never forwarded without the credential.

`self` (SDLC #637) is the platform's `agent-control` service in `tenant-apps`:
plain HTTP inside the cluster, `allow_private: true` and no credential. The
broker forwards whatever paths agent-control serves; the service, its RBAC and
its own ingress policy are owned by the agent-control change. The namespace
boundary is the caller authentication, exactly as for the rest of the broker.

The private enrollments (`loki`, `memory`, `status`) are the only exception to
the TLS rule: they never leave the cluster network and their credentials are
already cluster-local. A credentialed enrollment over plain HTTP is otherwise
rejected at load time. `self` carries no credential, so the exception does not
apply to it.

## Per-host request predicates

Every enrollment may carry predicates (SDLC #624). All of them are optional and
every default is empty, so an enrollment without them forwards exactly what it
did before the feature existed.

| Key | Meaning |
| --- | --- |
| `allow_methods` | Non-empty list: the request method must be in it. |
| `deny_methods` | The request method is refused. A method cannot be in both lists. |
| `allow_paths` | Non-empty list: the upstream path must `fullmatch` one regex. |
| `deny_paths` | The upstream path is refused when a regex matches its start (prefix). |
| `deny_body_patterns` | The body (UTF-8, undecodable bytes replaced) is refused when a regex `search` matches. |
| `body_guards` | Conditional JSON gate, described below. |
| `max_body_bytes` | Per-host cap, 1..64 MiB; a larger `Content-Length` is `413` before the body is read. |

`allow_paths` and a guard's `paths` match the **normalized** upstream path
(`posixpath.normpath` of everything after the logical host), so `/<host>/a/../b`
is checked as `/b` and cannot dodge a prefix. `deny_paths` is a prefix match so
a reviewed `^/admin(/|$)` denies the subtree while a near-miss
(`/administrators`) does not match by accident.

A denial is `403` with `broker: <reason>` and no body detail, and it happens
before DNS resolution, before connecting and before credential injection. The
reason is a fixed token: `method_denied`, `path_denied`, `body_denied`,
`body_json_invalid`, `body_field_denied`, or `request_too_large` (413).
Patterns are reviewed config, and the per-host body cap bounds the work they can
do on agent-supplied input.

The one conditional rule is `body_guards`: each entry names `methods` (empty
means any), one or more `paths` regexes, a `field` (dot-separated JSON keys) and
an `allow` regex. When a request matches the method and path, the body must be
valid UTF-8 JSON with no repeated key, and the named field must be a string
matching `allow` at its start. A repeated key is `body_json_invalid` because two
JSON parsers can read two different values from it. The canonical use is webhook
creation on the `github-hooks` host (SDLC #628):

```json
"body_guards": [
  {
    "methods": ["POST"],
    "paths": ["^/repos/[^/]+/[^/]+/hooks$"],
    "field": "config.url",
    "allow": "^https://agent-hooks\\.sammohr\\.dev/"
  }
]
```

`https://agent-hooks.sammohr.dev.evil.example/` and
`https://evil.example/?https://agent-hooks.sammohr.dev/` both fail: `allow` is
anchored at the start of the value (`re.match`), so only a URL that begins with
the enrolled origin passes.

## DNS self-resolution and IP pinning

For each request the broker resolves the enrolled host exactly once, checks
**every** address in the answer, and dials the address it checked. TLS uses that
address with the enrolled hostname for SNI and certificate verification, so the
name-to-address decision and the connection cannot disagree.

Refused for every enrollment, including a private one:

- the k3s pod CIDR `10.42.0.0/16` (the agent's own network);
- loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`,
  including the cloud metadata address), multicast, unspecified, reserved;
- CGNAT `100.64.0.0/10`;
- any address listed in `BROKER_DENY_ADDRESSES` (the node's real address);
- IPv4-mapped IPv6 forms of all of the above (`::ffff:10.42.0.9`);
- loopback names (`localhost`, `ip6-localhost`);
- any address that is not global unicast (TEST-NET, documentation and
  benchmarking ranges included).

Refused unless the enrollment is one of the private ones (`loki`, `status`,
`self`): RFC1918
(`10/8`, `172.16/12`, `192.168/16`), the service CIDR `10.43.0.0/16`, IPv6 ULA
(`fc00::/7`). A DNS answer that mixes a public and a private address is a
denial, not a fallback to the public one: that mix is the rebinding signature.
A DNS failure, empty answer or filtered address is a denial, never a second
lookup.

## Redirects

The broker never follows a redirect. A 3xx with a `Location` on a different real
host, port or scheme is refused with 502 and logged as `cross_host_redirect`;
a relative or same-endpoint redirect is returned to the agent untouched. The
agent must issue a new broker request naming a logical host, which is a fresh
enrollment lookup and a fresh injection.

## Logging and redaction

Each request writes one line at INFO:

```text
logical=<host> method=<method> path=<path> status=<status> bytes=<n> outcome=<proxied|denied|error>[ reason=<token>]
```

- The path is the broker path, **without the query string** (search queries
  stay out of logs). Path and denial text are redacted against every known
  credential.
- `reason=` appears only on a denial and is the fixed token from the request
  predicate or target check; it never contains the body, a header or a URL.
- No header value, request body or response body is logged. Upstream failures
  log the exception class only.
- Response headers that can carry a URL or a reflected credential (`Location`,
  `Content-Location`, `Link`, `Refresh`, `Set-Cookie`, `WWW-Authenticate`,
  `Proxy-Authenticate`) are redacted.
- Response bodies up to 1 MiB are buffered and redacted; beyond the cap the
  broker streams and marks `X-Broker-Redaction: partial` rather than claiming
  full coverage. The broker asks upstreams for `Accept-Encoding: identity` so a
  compressed body cannot hide a known secret from the matcher.
- Redaction replaces exact values and their URL, URL-plus, JSON, escaped-slash,
  HTML and base64 encodings with `[REDACTED:<logical-host>]`. A provider that
  transforms or re-encrypts a secret can still echo it undetected; that is the
  documented residual (spec §6.7), not a guarantee.

## Address policy in the CNI

The broker's own check is authoritative; `NetworkPolicy` is the coarse layer.
It bounds broker traffic only: the tenant also has its own direct public egress
(`platform/network/tenant-egress.yaml`, `docs/tenancy.md`), so a call that
skips the broker gets none of the guarantees below — no address policy, no
redirect refusal, no credential stripping, no redaction.

- `broker` egress: TCP 443 to global unicast only (`0.0.0.0/0` with every
  non-global IPv4 range in `except`, and the IPv6 equivalents), plus TCP 3100
  to `loki-gateway` and 9101 to `status-exporter` (`observability/network/`),
  plus TCP 8081 to `tenant-apps` for the `self` host
  (`broker/egress-agent-control.yaml`) and 8182 to `graphiti/memory-mcp` for the
  `memory` host (`broker/egress-memory.yaml`, paired with
  `platform/network/data-flows.yaml`'s `memory-mcp-from-broker`). The broker
  namespace is an egress allowlist: an unlisted destination is dropped by the
  CNI before the broker's own check sees it.
- `broker` ingress: tenant tier on 8080 only (`platform/network/tenant-broker.yaml`).
- DNS: `kube-system` on 53 (`platform/network/dns-egress.yaml`).
- Tunnel: `ingress/cloudflared` to `tenant-apps/nanobot` on 8080 only
  (`platform/network/agent-ingress.yaml`), paired with the tenant-side rule in
  `homelabs-apps`.

## Agent identity and debug RBAC

`platform/rbac/agent-nanobot.yaml` creates ServiceAccount `nanobot` in
`tenant-apps` with `automountServiceAccountToken: false`. The trusted controller
is the platform's own Flux `kustomize-controller`: the tenant reconciler has no
`serviceaccounts` rule, so the tenant cannot mint an identity or a token. The
admission policy `agent-identity` pins `serviceAccountName: nanobot` and
`automountServiceAccountToken: false` for every nanobot-labelled pod, and
`broker-runtime` pins gVisor, non-root and read-only rootfs for the broker.

In each data namespace (`immich`, `navidrome`, `opdshelf`, `graphiti`) Role
`agent-debug` grants exactly:

| Resource | Verbs |
| --- | --- |
| `events` | `get`, `list` |
| `pods` | `delete` (restart only) |

Everything else is absent on purpose: no `pods/log`, `pods/exec`,
`pods/attach`, `pods/portforward`, `pods/proxy`, no `get`/`list pods` (the
status exporter answers state), no secrets or configmaps, no
`patch`/`update deployments`, no `nodes` or `nodes/metrics` (the broker's
`status` logical host is the only metrics path).

The pod carries no token and its NetworkPolicy has no route to the API server,
so the grant is a declared ceiling for a short-lived, human-minted token
(`kubectl -n tenant-apps create token nanobot --duration=15m`) used for a
break-glass restart. It is not a credential the agent can reach on its own.

## Routing contract: `agent.sammohr.dev`

The hostname terminates on the **cluster** tunnel, not the host tunnel:

```text
agent.sammohr.dev --Access--> cluster tunnel --HTTP--> nanobot.tenant-apps.svc:80 --> pod:8080
```

- Access application `agent` exists in `tofu/access.tf` with the owner's email
  policy (precedence 1) and the `service auth` policy (precedence 2,
  `any_valid_service_token`). No bypass.
- maki uses an account service token in `cloudflared access`; the owner uses
  the browser identity. Access is evaluated at the edge, before the tunnel.
- `nanobot.sammohr.dev` is retired (SDLC #583). Its Access application was
  removed from `tofu/access.tf`; the operator runs `tofu state rm` first because
  the block carried `prevent_destroy` (`docs/manual-steps.md` step 20).
- nanobot holds **no Cloudflare token**: the tunnel token lives in `ingress`,
  and the agent's egress policy admits the broker and DNS only.

### OpenTofu: `tofu/cluster.tf`

The tunnel config is managed, not dashboard-only. `tofu/cluster.tf` maps every
`var.cluster_hosts` entry to one ingress rule through `local.cluster_ingress`
and appends the `http_status:404` catch-all. The plan refuses a `cluster_hosts`
entry with no rule, so a DNS cutover cannot leave a name on the catch-all.

```hcl
# tofu/cluster.tf, excerpt
locals {
  cluster_ingress = {
    agent = "http://nanobot.tenant-apps.svc.cluster.local:80"
  }
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "cluster" {
  count      = var.cluster_tunnel_id == "" ? 0 : 1
  account_id = var.account_id
  tunnel_id  = var.cluster_tunnel_id
  config = {
    ingress = concat(local.cluster_ingress_rules, [{ hostname = null, service = "http_status:404" }])
  }
}
```

`tofu/dns.tf` creates the proxied CNAME per `cluster_hosts` entry, and a
specific record wins over `*.sammohr.dev`. The apply is:

```sh
tofu -chdir=tofu plan -no-color    # one CNAME + the tunnel config, no replacements
tofu -chdir=tofu apply -no-color
```

Verification after apply:

```sh
dig +short agent.sammohr.dev            # CNAME <cluster-uuid>.cfargotunnel.com
curl -sS -o /dev/null -D - https://agent.sammohr.dev/ | head -3
# expect HTTP/2 302 to *.cloudflareaccess.com, never an origin response
```

### Repository mirror cross-reference

The platform's `github-backup` CronJob (`clusters/homelab/platform/backup/`,
`docs/runbook.md` "Repository mirror") uses a separate operator-owned
fine-grained read-only PAT, not a broker logical host. The broker mints its own
installation tokens from the app; the mirror's PAT is scoped to `Contents:
read` on the two repositories and is not the agent's identity.

### GitHub App identity

The broker holds `homelabs-agent`'s private key and mints installation
tokens on demand (`apps/broker/broker/github_app.py`): an RS256 JWT
(`iat` −60 s, `exp` +540 s, `iss` = app id) is exchanged at
`POST /app/installations/{id}/access_tokens` with the enrollment's
`repositories` and `permissions` subset. Tokens live one hour; the broker
reuses one until 5 minutes before expiry, keyed by installation, repositories
and permissions, and includes the live tokens in its redaction set. One
installation covers both repositories; the split between the three GitHub
logical hosts is the requested scope, not a separate install. A minting
failure is a `503` for that request — never a forward without a credential.

The app's permissions are app-wide, so the per-host split above is enforced by
these minted scopes plus the reviewed `broker.json`. The trust boundary is
unchanged: the broker was already the only credential holder; now the credential
is short-lived and revocable from the app's settings page.

## Inbound webhooks: `agent-hooks.sammohr.dev`

The broker is egress-only and the agent has no inbound listener. Deliveries
terminate on the platform-owned receiver in the `webhook` namespace
(`clusters/homelab/webhook/`, `apps/webhook/`): an unguessable capability URL,
an optional GitHub HMAC, a 1 MiB limit and delivery-id dedupe, then one
enqueue call to this gateway's WebSocket listener on 8765. The hostname is an
Access bypass by construction and is not rate limited. Contract, threat model
and the researched gateway frames: `docs/webhook.md`.

## Verification

```sh
# broker unit tests (no network, no cluster)
python3 -m unittest discover -s apps/broker/tests -t apps/broker -v

# static policy, RBAC, network and broker-manifest assertions
tests/policies/check.sh

# admission and can-i probes on the throwaway apiserver
nix shell nixpkgs#etcd nixpkgs#kubernetes nixpkgs#openssl nixpkgs#kustomize \
  nixpkgs#yq-go nixpkgs#jq -c tests/policies/check.sh --runtime
```

The runtime probe evaluates `system:serviceaccount:tenant-apps:nanobot` against
every row in `tests/policies/rbac.tsv` marked `agent`: events and `delete pods`
are the only `yes` answers; secrets, `pods/log`, exec, attach, portforward,
proxy, deployment patches and nodes are all `no`.

Live-gated (need the cluster, the real tokens and the operator):

- publishing the broker image and replacing the digest placeholder;
- creating `broker-credentials` from SOPS (manual steps);
- the broker's first real call to each logical host (GitHub push, platform PR,
  the `sdlc` registry push/issues, Neuralwatt, TinyFish, Loki, memory, notify,
  agent-control);
- the notify bus's first real publish (the enrollment now names
  `ntfy.sammohr.dev`; the token is the `agent` user's);
- agent-control's Service, RBAC and tenant-side ingress
  (`tenant-apps` -> port 8081);
- tunnel/Access routing and the `nanobot.sammohr.dev` retirement;
- NetworkPolicy and gVisor enforcement (no CNI/kubelet on the test bench).
