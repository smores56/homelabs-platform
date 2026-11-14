# Tenancy

Twelve namespaces carry every workload. Each is labelled with the owning tenant, a
tier, and PSA enforcement, so admission policy, RBAC scope and NetworkPolicy
all select by label rather than by hard-coded name. The data tier is one
namespace per application (smores56/sdlc#580), because each app is bound to one
virtiofs share and must not be able to reach another app's.

| Namespace | `homelabs.dev/tenant` | `homelabs.dev/tier` | PSA enforce | Contents |
| --- | --- | --- | --- | --- |
| `flux-system` | `platform` | `platform` | `baseline` | Flux controllers (labels added by the `flux-system-namespace` patch) |
| `platform` | `platform` | `platform` | `baseline` | ESO, policy controllers, platform jobs |
| `tenant-apps` | `apps` | `tenant` | `restricted` | `homelabs-apps` workloads (nanobot and successors) |
| `broker` | `broker` | `broker` | `restricted` | Egress broker with per-host tokens |
| `webhook` | `webhook` | `webhook` | `restricted` | Inbound webhook receiver; capability URLs enqueue into the agent (`docs/webhook.md`) |
| `notify` | `notify` | `notify` | `restricted` | ntfy: the notification bus for the phone, the laptop, host units, Alertmanager, Flux and the agent (`docs/notify.md`) |
| `immich` | `immich` | `data` | `restricted` | Immich server, machine-learning, Postgres, Valkey |
| `navidrome` | `navidrome` | `data` | `restricted` | Navidrome |
| `opdshelf` | `opdshelf` | `data` | `restricted` | OPDShelf over the books library |
| `graphiti` | `graphiti` | `data` | `baseline` | Graphiti (root start for `uv`) and FalkorDB |
| `ingress` | `ingress` | `ingress` | `restricted` | `cloudflared` for the cluster tunnel |
| `observability` | `observability` | `observability` | `privileged` | Prometheus, Loki, Vector, status exporter |

`platform`/`flux-system` are `baseline` because the reconcilers hold cluster-wide
permissions; audit and warn still record `restricted` violations there.
`observability` enforces `privileged` because node-exporter needs `hostPath` and
`hostNetwork`, which baseline forbids; audit and warn stay `restricted`.
`opdshelf` is `restricted` again now that OPDShelf replaced the s6-backed
Calibre-Web-Automated image: it runs as uid 1000 with dropped capabilities and
`RuntimeDefault` seccomp, so it needs no exemption.
`tenant-apps` was previously named `tenant`; the rename came with the tenant
reconciler, which owns that namespace end to end.

Definitions live in `clusters/homelab/platform/namespaces/namespaces.yaml` and are
applied by the first stage (`docs/flux.md`).

## What each tier may do

| Tier | Admission policy | Egress | Ingress |
| --- | --- | --- | --- |
| `platform` | exempt from `no-host-access` | unrestricted | unrestricted |
| `tenant` | gVisor, digest-pinned images, scratch PVC only, no `sops-age`, no reconciler SA, `agent-identity`, `nanobot-policy` | DNS, the broker on 8080, direct TCP/UDP to global unicast (every non-global range excluded), the k3s API (443/6443) for `agent-control` only | the cluster tunnel on 8080 (`nanobot` only); the broker on 8081 (`agent-control` only) |
| `webhook` | `no-host-access`, `webhook-runtime` | DNS, `tenant-apps/nanobot` 8765 only | the cluster tunnel on 8080 |
| `notify` | `no-host-access`, `notify-runtime` | DNS only | the cluster tunnel on 8080, `observability/notifyrelay` on 8080, `observability/prometheus` on 9090 |
| `data` (each of `immich`, `navidrome`, `opdshelf`, `graphiti`) | `no-host-access`, `data-workload-source`, `data-pvc-scope`, `data-pvc-storage` | same namespace, DNS; HTTPS for `immich-machine-learning` and `graphiti` | same namespace, `ingress` on the app's port |
| `ingress` | none beyond PSA | the four data apps and `graphiti/memory-mcp` on their ports, `tenant-apps/nanobot` 8080, Cloudflare edge 443, DNS | none |
| `observability` | exempt from `no-host-access` | DNS, the k3s API range on 443 (Vector, Prometheus, exporter), Loki and the annotated scrape targets, Prometheus → Alertmanager, Alertmanager → mail relay; the mail relay alone reaches 443 on global unicast | default-deny; Vector/Loki/gateway wiring, Prometheus scrapes, the broker paths, and the alerting chain |

## Admission policy

Seventeen policies, each in its own file with the binding that scopes it
(`clusters/homelab/platform/policies/`, asserted by `tests/policies/check.sh`).

| Policy | Bound to | Rejects |
| --- | --- | --- |
| `no-host-access` | `tenant`, `broker`, `data` (all four namespaces), `webhook`, `notify` | `hostNetwork`, `hostPID`, `hostIPC`, `hostPath`, privileged containers, `hostPort`, added capabilities, `allowPrivilegeEscalation` |
| `tenant-gvisor` | `tenant` | any pod without `spec.runtimeClassName: gvisor` |
| `tenant-images` | `tenant` | images not pinned by `@sha256:`, floating tags, nanobot from outside `ghcr.io/smores56/homelabs-platform/nanobot@sha256:` |
| `tenant-scratch-pvc` | `tenant` | pod volumes whose PVC name does not start with the pod's `app.kubernetes.io/name` |
| `tenant-pvc-naming` | `tenant` | PVCs without an `app.kubernetes.io/name` label or not named `<name>-*` |
| `tenant-flux-guard` | `tenant` | Kustomizations that use another service account, target another namespace, read another GitRepository, carry a `kubeConfig`, or decrypt with another age key |
| `tenant-flux-sa-pods` | `tenant` | pods running as `tenant-apps-reconciler` |
| `tenant-secret-scope` | `tenant` | pods mounting or reading the `sops-age` Secret |
| `nanobot-policy` | `tenant` | nanobot pods that omit the `nanobot-policy` ConfigMap mount, place another volume under `/etc/nanobot/{prompts,models}`, or set `NANOBOT_CONFIG_DIR` to anything but `/etc/nanobot` |
| `nanobot-policy-write` | `tenant` | writes to the platform-owned `nanobot-policy` ConfigMap by anyone but the platform reconciler |
| `data-workload-source` | `data` (all four) | pods without `homelabs.dev/managed-by=platform` or not running as `data-workload` |
| `data-pvc-scope` | `data` (all four) | pods mounting a PVC whose name does not start with their own namespace, i.e. another app's share |
| `data-pvc-storage` | `data` (all four) | PVCs not named `<namespace>-*` or not claiming `data-local` (no dynamic provisioning in the data tier) |
| `webhook-runtime` | `webhook` | pods without gVisor, `runAsNonRoot` or a read-only rootfs (the publicly reachable receiver) |

All policies use `failurePolicy: Fail`. Object policies cannot read who sent a
request, so creator identity is enforced by RBAC (below) and by pinning the label
and service account a tenant-authored manifest would have to fake. The one
identity check is `nanobot-policy-write`, which reads `request.userInfo` to keep
the platform ConfigMap out of the tenant reconciler's reach.

PSA is the first line and overlaps `no-host-access` in the restricted tiers on
purpose: if a namespace's PSA label drifts, the admission policy still rejects
host access.

## Tenant RBAC

`tenant-apps-reconciler` is a Role in `tenant-apps`, bound only to itself:

- **granted**: pods (and `pods/log` reads), services, configmaps,
  persistentvolumeclaims, events, deployments, statefulsets, jobs, cronjobs,
  endpointslices;
- **denied**: secrets, serviceaccounts, roles, rolebindings, resourcequotas,
  limitranges, networkpolicies, ingresses, poddisruptionbudgets, every
  `*.toolkit.fluxcd.io` and `external-secrets.io` resource, and all cluster-scoped
  resources (namespaces, nodes, runtimeclasses, CRDs, VAPs, storageclasses);
- **escalate/bind/impersonate**: never granted, and unreachable because RBAC
  writes on RBAC objects are absent.

That Role is the identity kustomize-controller impersonates for the
`homelabs-apps` Kustomization, so the tenant owns its workload objects and
nothing else. `can-i` expectations live in `tests/policies/rbac.tsv` and run
against a real API server in `--runtime` mode.

`data-workload` is the only service account data pods may use, and it exists in
each of the four data namespaces (a service account is namespaced). The tenant
identity has no binding in any data namespace, `broker` or `platform`.

`nanobot` is the agent's own ServiceAccount in `tenant-apps`, created by the
platform RBAC stage with `automountServiceAccountToken: false`. It is bound to
`agent-debug` in each data namespace, which grants only `get`/`list` on events
and `delete` on pods. There is no `get pods`, `pods/log`, exec, attach,
portforward, proxy, secrets, deployment patch, node or node-metrics rule, and
the pod has no token or API route; the grant is a break-glass ceiling
(`docs/broker.md`).

## Agent cluster access

`agent-control` (`clusters/homelab/agent-control`) is the one identity that talks
to the Kubernetes API on the agent's behalf, and the only tenant pod with an
automounted token: `agent-identity` still pins
`automountServiceAccountToken: false` for every nanobot-labelled pod, so the
token cannot be copied into the agent. It is the **only cluster API capability
the agent gets** — there is no kubeconfig, no proxy and no generic forwarding.

| Piece | Where | Ceiling |
| --- | --- | --- |
| Service | `tenant-apps/agent-control`, ClusterIP **8081** | ingress only from the `broker` namespace on 8081; egress only DNS and the k3s API range (443/6443) |
| Role | `tenant-apps/agent-control` (platform RBAC stage) | `pods`, `pods/log`, `events`, `replicasets` get/list/watch; `get`/`patch` on `deployments/nanobot` only (`resourceNames`) |
| ClusterRole | `agent-control-read`, bound in the agent-control stage | `pods`, `events`, `nodes` get/list/watch; `deployments`, `statefulsets`, `daemonsets` get/list; Flux `kustomizations` get/list |
| Secret | `tenant-apps/agent-control-build` (hand-made, step 33), key `token`, mounted only at `/run/build` | a GitHub App installation token or fine-grained PAT with `Actions: write` on `smores56/homelabs-platform` only — **the only GitHub credential outside the broker**; mounted `optional: true`, so its absence degrades `POST /self/build` to `503` and nothing else |

The HTTP surface is exactly ten routes; any other path is `404` and a wrong
method on a known path is `405`. Probes are TCP checks, so no HTTP health route
exists outside the list.

| Route | Does |
| --- | --- |
| `GET /self/status` | the nanobot Deployment's replica counts and its own pods (status only) |
| `GET /self/logs?tail=N` | container logs, `tail` clamped to 1..500, only for pods labelled `app.kubernetes.io/name=nanobot` **and** owned by the nanobot Deployment's ReplicaSets |
| `GET /self/events` | events about those pods and the Deployment |
| `POST /self/restart` | patches the `homelabs.dev/restart-requested-at` pod-template annotation on `deployments/nanobot` (`202`) |
| `POST /self/build` | dispatches `publish-nanobot.yml` through the Actions API (`202`); repository, workflow and ref are constants, the token is read from the mounted Secret and never logged (`docs/flux.md`) |
| `GET /cluster/pods`, `/cluster/events`, `/cluster/nodes` | read-only cluster picture, curated fields |
| `GET /cluster/workloads` | Deployments/StatefulSets/DaemonSets across namespaces, **metadata only** |
| `GET /cluster/flux` | Flux Kustomizations with readiness, revision and message |

Secrets and ConfigMaps appear in no rule, and the client refuses those paths
before it builds a request. Pod specs never leave the service (no env, no secret
references); workload answers are metadata; Kustomization answers drop `spec`.
Every request prints one audit line to stdout
(`level=audit method=… path=… status=… caller=… duration_ms=…`); headers, tokens
and bodies are never logged.

The broker reaches it through its `self` logical host
(`agent-control.tenant-apps.svc.cluster.local:8081`), so the agent never holds
the Service's address, let alone the token. `docs/manual-steps.md` step 26 is the
post-apply verification.

## NetworkPolicy

| Namespace | default-deny | Allowed |
| --- | --- | --- |
| `tenant-apps` | both directions | DNS to `kube-system`; TCP 8080 to `broker` tier; direct TCP/UDP to global unicast (`0.0.0.0/0` and `::/0` with every non-global range in `except`); TCP 8081 from the `broker` namespace (`agent-control` only); TCP 443/6443 to the k3s API range (`agent-control` only); ingress from `ingress/cloudflared` on 8080 (nanobot only) |
| `broker` | both directions | DNS; ingress from `tenant` tier on 8080; egress TCP 443 to global unicast only (every non-global range excluded), TCP 3100 to `loki-gateway` and 9101 to `status-exporter` in `observability`, 8081 to `tenant-apps/agent-control` and 8182 to `graphiti/memory-mcp` |
| `immich` | both directions | DNS; same-namespace any port; TCP 2283 from `ingress`; HTTPS from the machine-learning pod for model weights |
| `navidrome` | both directions | DNS; same-namespace any port; TCP 4533 from `ingress` |
| `opdshelf` | both directions | DNS; same-namespace any port; TCP 8083 from `ingress` |
| `graphiti` | both directions | DNS; same-namespace any port; TCP 8182 from the host tap addresses (the memory wrapper's NodePort), from `ingress/cloudflared` (the cluster tunnel, SDLC #617) and from the `broker` namespace (the `memory` logical host, SDLC #631); HTTPS from the graphiti pod to the Neuralwatt API; TCP 443 to R2 for the restic job |
| `ingress` | both directions | DNS; the three published app Services on their ports; `tenant-apps/nanobot` on 8080; TCP 443 to the Cloudflare edge (`cloudflared` only) |

Adding an app flow means adding a policy with its exact port and namespace
selector. Nothing matches all namespaces, and a missing allow fails closed.

### The tenant's public egress

`egress-public` (`platform/network/tenant-egress.yaml`) gives the tenant a direct
path out: every TCP and UDP port to global unicast, with `10/8`, `172.16/12`,
`192.168/16`, `169.254/16`, `100.64/10` and `127/8` excluded from IPv4 and
`fc00::/7`, `fe80::/10` and `::1/128` from IPv6. The exclusions are what keep a
public destination from being rewritten into the pod or service network, the
tap, the host's LAN, the cloud metadata address or loopback. The broker rule
stays: anything that needs a credential injected, a name allowlist or a
response redaction still goes through it.

Deliberately absent: no proxy, no rate limit, no bandwidth cap, no per-destination
allowlist, no kill switch, and no content inspection. This is an address filter,
not DLP — a byte that leaves the tenant through this rule is not recorded except
as a flow. There is no DNS query log either: CoreDNS logging is not enabled, and
the same rule admits DoH/DoT to a public resolver, so even a future log would be
best-effort and bypassable, never an audit trail. Two layers bound it in practice: the host's tap firewall
(`modules/nixos/cluster-vm.nix` in nix-config) admits only TCP 80/443, 7844, DNS
and NTP out of the guest, and that file is owned by the host lane; until it is
widened, only those ports actually reach the internet. Neither layer is
something the tenant can change.

The tenant repository keeps a mirrored copy of its NetworkPolicies as the
reviewed contract (`homelabs-apps`, `deploy/`). That copy must carry the same
two `ipBlock` entries; `tests/policies/check.sh` asserts the platform side only,
because the tenant repository is not readable from this one.

`observability` is default-deny in both directions too, with one file per flow in
`clusters/homelab/observability/network/`: Vector to Loki 3100, the gateway to
Loki 3100 (Loki accepts ingress from exactly those two pods), Prometheus to the
annotated targets, the broker to the gateway and the exporter, the cluster digest
to those two (stated in the digest stage), the k3s API range on 443 for the three
API clients, and Prometheus → Alertmanager → mail relay. Only the mail relay has
public egress, and only on 443 (`docs/observability.md`). `platform` is the one
namespace still without a default-deny: it holds the CronJobs and the reconciler
pods, which are platform-tier by label and already cluster-wide.

An agent reaches the observability stack only through the broker (the Loki
gateway for `data` reads, the status exporter for metrics), and both paths are
stated on both ends.

## Secrets

Two mechanisms, no third:

1. **SOPS + age, per namespace.** Any encrypted manifest is decrypted by
   kustomize-controller from a `sops-age` Secret in the namespace of the
   Kustomization that carries it (`decryption.secretRef.name: sops-age`). The
   controller-wide `--sops-age-secret` flag is deliberately unset, so a
   namespace without the Secret fails instead of borrowing another key.
   `tenant-apps` carries the tenant ciphertext; `broker` holds the hand-made
   `broker-credentials` Secret (eight credential files, two of them optional,
   never a manifest); the
   `webhook` namespace holds the hand-made `webhook-tokens` Secret (admin and
   enqueue tokens, `docs/webhook.md`). The contract is spelled out in the
   `sops-contract` ConfigMaps in `flux-system`, `broker` and `webhook`, and in
   `docs/runbook.md`.
2. **External Secrets, two shapes.** `platform/secrets/eso/` holds a
   namespaced `SecretStore`, a `Password` generator and `ExternalSecret`s. No
   credential is committed. Those objects are applied by the
   `platform-secrets-eso` Kustomization, which requires the operator (and its
   CRDs), and that operator is not installed by this repository
   (`docs/runbook.md`).
   - *Generated*: `immich-db`, `loki-gateway-token` — a `Password` generator
     mints the value once. The agent has no broker token; caller identity is
     the namespace boundary (`docs/broker.md`).
   - *Contract*: `neuralwatt-api-key` (in `graphiti`) and
     `cloudflare-tunnel-token` (in `ingress`) — ESO's `kubernetes` provider
     projects a SOPS+age Secret (`*-sops`, created by hand, see
     `docs/manual-steps.md` step 12) under the name the workload reads. The
     ciphertext is the only source of truth and the projection is what a
     rotation re-reads (1h refresh).

The `sops-age` key is also why `tenant-secret-scope` exists: the tenant can create
pods, so without that rule it could mount the age identity and decrypt every
SOPS file in its namespace.

## Contract with `homelabs-apps`

The tenant repository is reconciled by `tenant-apps-reconciler` with
`targetNamespace: tenant-apps` from `./deploy`, so:

- it must not declare `Namespace` objects (namespaces are platform-owned and the
  tenant identity has no cluster-scoped create);
- every workload carries `app.kubernetes.io/name`, which selects the gVisor
  requirement, the PVC naming rule, the nanobot registry rule and its scratch
  claim;
- images are digest-pinned; PVCs are named `<app.kubernetes.io/name>-*`;
- egress is the broker on 8080, DNS, and direct TCP/UDP to global unicast; a
  call that needs a broker credential still needs a broker route, and nothing
  else in the cluster is reachable from the tenant;
- secrets come from `sops-age`-decrypted files or ESO generators, never from a
  float in git.
