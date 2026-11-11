# Tenancy

Five namespaces carry every workload. Each is labelled with the owning tenant
and tier, and PSA enforcement, so a policy can select by label rather than by
hard-coded name.

| Namespace | `homelabs.dev/tenant` | `homelabs.dev/tier` | PSA enforce | Contents |
| --- | --- | --- | --- | --- |
| `platform` | `platform` | `platform` | `baseline` | Flux, ESO, policy controllers, github-backup |
| `tenant` | `apps` | `tenant` | `restricted` | nanobot and other agent workloads from `homelabs-apps` |
| `broker` | `broker` | `broker` | `restricted` | Egress broker with injected per-host tokens |
| `data` | `data` | `data` | `restricted` | Immich, Navidrome, Calibre, Graphiti |
| `observability` | `observability` | `observability` | `restricted` | Prometheus, Loki, Vector, status exporter |

`platform` is `baseline` because Flux, ESO and the policy controllers run as
cluster-scoped reconcilers; `audit` and `warn` still record `restricted`
violations there. Every other namespace enforces `restricted` outright.

Labels are the contract later tasks build on:

- tenant Kustomizations select `homelabs.dev/tenant` to scope reconciliation,
  and use per-tenant `decryption.secretRef` for age keys;
- ValidatingAdmissionPolicies select `homelabs.dev/tier` to force the `gvisor`
  RuntimeClass for agent workloads and to switch on default-deny plus
  broker-only egress for non-platform tiers;
- `data` additionally gets default-deny ingress and egress for human-owned apps.

Namespace definitions live in `clusters/homelab/platform/namespaces.yaml`;
Flux applies that directory before any tenant kustomization, since every tenant
manifest needs its namespace labels in place first.
