# Tenancy

Ten namespaces carry every workload. Each is labelled with the owning tenant, a
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
| `immich` | `immich` | `data` | `restricted` | Immich server, machine-learning, Postgres, Valkey |
| `navidrome` | `navidrome` | `data` | `restricted` | Navidrome |
| `calibre` | `calibre` | `data` | `restricted` | Calibre-Web over the books library |
| `graphiti` | `graphiti` | `data` | `restricted` | Graphiti and FalkorDB |
| `ingress` | `ingress` | `ingress` | `restricted` | `cloudflared` for the cluster tunnel |
| `observability` | `observability` | `observability` | `privileged` | Prometheus, Loki, Vector, status exporter |

`platform`/`flux-system` are `baseline` because the reconcilers hold cluster-wide
permissions; audit and warn still record `restricted` violations there.
`observability` enforces `privileged` because node-exporter needs `hostPath` and
`hostNetwork`, which baseline forbids; audit and warn stay `restricted`.
`tenant-apps` was previously named `tenant`; the rename came with the tenant
reconciler, which owns that namespace end to end.

Definitions live in `clusters/homelab/platform/namespaces/namespaces.yaml` and are
applied by the first stage (`docs/flux.md`).

## What each tier may do

| Tier | Admission policy | Egress | Ingress |
| --- | --- | --- | --- |
| `platform` | exempt from `no-host-access` | unrestricted | unrestricted |
| `tenant` | gVisor, digest-pinned images, scratch PVC only, no `sops-age`, no reconciler SA, `agent-identity` | broker only, plus DNS | the cluster tunnel on 8080 (`nanobot` only) |
| `broker` | `no-host-access`, `broker-runtime` | public TCP 443 (global unicast only), DNS, `loki-gateway` 3100 and `status-exporter` 9101 | `tenant` tier on 8080 |
| `data` (each of `immich`, `navidrome`, `calibre`, `graphiti`) | `no-host-access`, `data-workload-source`, `data-pvc-scope`, `data-pvc-storage` | same namespace, DNS; HTTPS for `immich-machine-learning` and `graphiti` | same namespace, `ingress` on the app's port |
| `ingress` | none beyond PSA | the four data apps on their ports, `tenant-apps/nanobot` 8080, Cloudflare edge 443, DNS | none |
| `observability` | exempt from `no-host-access` | unrestricted | unrestricted |

## Admission policy

Thirteen policies, each in its own file with the binding that scopes it
(`clusters/homelab/platform/policies/`, asserted by `tests/policies/check.sh`).

| Policy | Bound to | Rejects |
| --- | --- | --- |
| `no-host-access` | `tenant`, `broker`, `data` (all four namespaces) | `hostNetwork`, `hostPID`, `hostIPC`, `hostPath`, privileged containers, `hostPort`, added capabilities, `allowPrivilegeEscalation` |
| `tenant-gvisor` | `tenant` | any pod without `spec.runtimeClassName: gvisor` |
| `tenant-images` | `tenant` | images not pinned by `@sha256:`, floating tags, nanobot from outside `ghcr.io/smores56/homelabs-apps/nanobot@sha256:` |
| `tenant-scratch-pvc` | `tenant` | pod volumes whose PVC name does not start with the pod's `app.kubernetes.io/name` |
| `tenant-pvc-naming` | `tenant` | PVCs without an `app.kubernetes.io/name` label or not named `<name>-*` |
| `tenant-flux-guard` | `tenant` | Kustomizations that use another service account, target another namespace, read another GitRepository, carry a `kubeConfig`, or decrypt with another age key |
| `tenant-flux-sa-pods` | `tenant` | pods running as `tenant-apps-reconciler` |
| `tenant-secret-scope` | `tenant` | pods mounting or reading the `sops-age` Secret |
| `data-workload-source` | `data` (all four) | pods without `homelabs.dev/managed-by=platform` or not running as `data-workload` |
| `data-pvc-scope` | `data` (all four) | pods mounting a PVC whose name does not start with their own namespace, i.e. another app's share |
| `data-pvc-storage` | `data` (all four) | PVCs not named `<namespace>-*` or not claiming `data-local` (no dynamic provisioning in the data tier) |

All policies use `failurePolicy: Fail`. CEL cannot read who sent a request, so
creator identity is enforced by RBAC (below) and the policies pin the label and
service account a tenant-authored manifest would have to fake.

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

## NetworkPolicy

| Namespace | default-deny | Allowed |
| --- | --- | --- |
| `tenant-apps` | both directions | DNS to `kube-system`; TCP 8080 to `broker` tier; ingress from `ingress/cloudflared` on 8080 (nanobot only) |
| `broker` | both directions | DNS; ingress from `tenant` tier on 8080; egress TCP 443 to global unicast only (every non-global range excluded), TCP 3100 to `loki-gateway` and 9101 to `status-exporter` in `observability` |
| `immich` | both directions | DNS; same-namespace any port; TCP 2283 from `ingress`; HTTPS from the machine-learning pod for model weights |
| `navidrome` | both directions | DNS; same-namespace any port; TCP 4533 from `ingress` |
| `calibre` | both directions | DNS; same-namespace any port; TCP 8083 from `ingress` |
| `graphiti` | both directions | DNS; same-namespace any port; TCP 8184 from `ingress`; HTTPS from the graphiti pod to the Neuralwatt API |
| `ingress` | both directions | DNS; the four app Services on their ports; `tenant-apps/nanobot` on 8080; TCP 443 to the Cloudflare edge (`cloudflared` only) |

Adding an app flow means adding a policy with its exact port and namespace
selector. Nothing matches all namespaces, and a missing allow fails closed.
`observability` and `platform` have no default-deny of their own: they are
platform-tier by label and hold cluster-wide permissions already. The two
observability paths an agent can reach are allow-listed from the broker side
(the Loki gateway and the status exporter) and stated on both ends in
`clusters/homelab/observability/network/`.

## Secrets

Two mechanisms, no third:

1. **SOPS + age, per namespace.** Any encrypted manifest is decrypted by
   kustomize-controller from a `sops-age` Secret in the namespace of the
   Kustomization that carries it (`decryption.secretRef.name: sops-age`). The
   controller-wide `--sops-age-secret` flag is deliberately unset, so a
   namespace without the Secret fails instead of borrowing another key.
   `tenant-apps` carries the tenant ciphertext; `broker` holds the hand-made
   `broker-credentials` Secret (five credential files, never a manifest); the
   contract is spelled out in the `sops-contract` ConfigMaps in `flux-system`
   and `broker`, and in `docs/runbook.md`.
2. **External Secrets, two shapes.** `platform/secrets/eso/` holds a
   namespaced `SecretStore`, a `Password` generator and `ExternalSecret`s. No
   credential is committed. Those objects are applied by the suspended
   `platform-secrets-eso` Kustomization, because the operator (and its CRDs) is
   not installed by this repository (`docs/runbook.md`).
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
- egress goes to the broker only, so new upstream calls need a broker route, not
  a NetworkPolicy change;
- secrets come from `sops-age`-decrypted files or ESO generators, never from a
  float in git.
