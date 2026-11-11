# Tenancy

Seven namespaces carry every workload. Each is labelled with the owning tenant,
a tier, and PSA enforcement, so admission policy, RBAC scope and NetworkPolicy
all select by label rather than by hard-coded name.

| Namespace | `homelabs.dev/tenant` | `homelabs.dev/tier` | PSA enforce | Contents |
| --- | --- | --- | --- | --- |
| `flux-system` | `platform` | `platform` | `baseline` | Flux controllers (labels added by the `flux-system-namespace` patch) |
| `platform` | `platform` | `platform` | `baseline` | ESO, policy controllers, platform jobs |
| `tenant-apps` | `apps` | `tenant` | `restricted` | `homelabs-apps` workloads (nanobot and successors) |
| `broker` | `broker` | `broker` | `restricted` | Egress broker with per-host tokens |
| `data` | `data` | `data` | `restricted` | Immich, Navidrome, Calibre, Graphiti |
| `ingress` | `ingress` | `ingress` | `restricted` | Cluster-side proxy that fronts data apps |
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
| `tenant` | gVisor, digest-pinned images, scratch PVC only, no `sops-age`, no reconciler SA | broker only, plus DNS | none |
| `broker` | `no-host-access` | provider APIs (platform-owned) | `tenant` tier on 8080 |
| `data` | `no-host-access`, platform-managed label + `data-workload` SA | same namespace, DNS | same namespace, `ingress` on immich 2283 |
| `ingress` | none beyond PSA | `data` immich 2283, DNS | none |
| `observability` | exempt from `no-host-access` | unrestricted | unrestricted |

## Admission policy

Nine policies, each in its own file with the binding that scopes it
(`clusters/homelab/platform/policies/`, asserted by `tests/policies/check.sh`).

| Policy | Bound to | Rejects |
| --- | --- | --- |
| `no-host-access` | `tenant`, `broker`, `data` | `hostNetwork`, `hostPID`, `hostIPC`, `hostPath`, privileged containers, `hostPort`, added capabilities, `allowPrivilegeEscalation` |
| `tenant-gvisor` | `tenant` | any pod without `spec.runtimeClassName: gvisor` |
| `tenant-images` | `tenant` | images not pinned by `@sha256:`, floating tags, nanobot from outside `ghcr.io/smores56/homelabs-apps/nanobot@sha256:` |
| `tenant-scratch-pvc` | `tenant` | pod volumes whose PVC name does not start with the pod's `app.kubernetes.io/name` |
| `tenant-pvc-naming` | `tenant` | PVCs without an `app.kubernetes.io/name` label or not named `<name>-*` |
| `tenant-flux-guard` | `tenant` | Kustomizations that use another service account, target another namespace, read another GitRepository, carry a `kubeConfig`, or decrypt with another age key |
| `tenant-flux-sa-pods` | `tenant` | pods running as `tenant-apps-reconciler` |
| `tenant-secret-scope` | `tenant` | pods mounting or reading the `sops-age` Secret |
| `data-workload-source` | `data` | pods without `homelabs.dev/managed-by=platform` or not running as `data-workload` |

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

`data-workload` is the only service account data pods may use; the tenant
identity has no binding in `data`, `broker` or `platform`.

## NetworkPolicy

| Namespace | default-deny | Allowed |
| --- | --- | --- |
| `tenant-apps` | both directions | DNS to `kube-system`; TCP 8080 to `broker` tier |
| `broker` | both directions | DNS; ingress from `tenant` tier on 8080; egress TCP 3100 to `loki-gateway` and 9101 to `status-exporter` in `observability` |
| `data` | both directions | DNS; same-namespace any port; TCP 2283 from `ingress` namespace to `app.kubernetes.io/name=immich` |
| `ingress` | both directions | DNS; TCP 2283 to `data` immich |

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
   `tenant-apps` is the only current consumer; the contract is spelled out in the
   `sops-contract` ConfigMap in `flux-system` and in `docs/runbook.md`.
2. **External Secrets, generated only.** `platform/secrets/eso/` shows a
   namespaced `SecretStore`, a `Password` generator and two `ExternalSecret`
   shapes. No credential is committed. Those objects are applied by the suspended
   `platform-secrets-eso` Kustomization, because the operator (and its CRDs) is
   not installed by this repository (`docs/runbook.md`).

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
