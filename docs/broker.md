# The agent broker

SDLC #582. nanobot has no credentials, no cluster token and no direct egress.
Every outbound request leaves through one broker Deployment in the `broker`
namespace; the broker maps an enrolled **logical host** to one real endpoint,
resolves and pins the address itself, strips whatever credential the agent sent,
injects exactly one enrolled credential, refuses redirects that leave the
enrolled endpoint, and logs metadata only.

Source: `apps/broker/` (Python stdlib, 77 unit tests). Manifests:
`clusters/homelab/broker/`. Applied by the Flux stage `platform-broker`.

## Trust boundary and non-goals

The broker is inside the boundary: it holds the provider and GitHub tokens and
is the only place they exist in the cluster. It is **not** an egress proxy and
**not** an action authorizer. It does not inspect method, path or payload for
allow/deny (Kanbei spec `04-security-deployment.md` §6.5), so an enrolled token
is a credential oracle limited only by provider-side authorization. What it
does enforce is target integrity: the agent names a logical host and a relative
path, never a hostname, IP, SNI or `Host`.

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
- The request body is read up to 64 MiB; a chunked request body is refused
  (411) rather than half-forwarded.
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
| `github-agent-push` | `https://github.com:443` | `Authorization: Basic x-access-token:<token>` | app: `nix-config`, `kanbei`, `contents: write` | no |
| `github-agent` | `https://api.github.com:443` | `Authorization: Bearer` | app: `nix-config`, `kanbei`, `contents: read`, `pull_requests: write` | no |
| `tinyfish` | `https://api.search.tinyfish.ai:443` | `X-Api-Key: <token>` | `tinyfish-api-key` | no |
| `loki` | `http://loki-gateway.observability.svc.cluster.local:3100` | `Authorization: Bearer`, fixed `X-Scope-OrgID: data` | `loki-gateway-token` | yes |
| `status` | `http://status-exporter.observability.svc.cluster.local:9101` | none (metrics text) | — | yes |

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

Every repository named on a host goes into a single token request, so one name
that is not in the app installation fails the whole host with a 422. Add a
repository only after confirming it is in the installation.

The two private enrollments are the only exception to the TLS rule: they never
leave the cluster network and their credentials are already cluster-local. A
credentialed enrollment over plain HTTP is otherwise rejected at load time.

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

Refused unless the enrollment is one of the two private ones: RFC1918
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
logical=<host> method=<method> path=<path> status=<status> bytes=<n> outcome=<proxied|denied|error>
```

- The path is the broker path, **without the query string** (search queries
  stay out of logs). Path and denial text are redacted against every known
  credential.
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

The broker's own check is authoritative; `NetworkPolicy` is the coarse layer:

- `broker` egress: TCP 443 to global unicast only (`0.0.0.0/0` with every
  non-global IPv4 range in `except`, and the IPv6 equivalents), plus TCP 3100
  to `loki-gateway` and 9101 to `status-exporter` (`observability/network/`).
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

In each data namespace (`immich`, `navidrome`, `calibre`, `graphiti`) Role
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
  Neuralwatt, TinyFish, Loki);
- tunnel/Access routing and the `nanobot.sammohr.dev` retirement;
- NetworkPolicy and gVisor enforcement (no CNI/kubelet on the test bench).
