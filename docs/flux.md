# Flux bootstrap and tenancy lockdown

The cluster is bootstrapped from this repository into one root Kustomization:

```text
GitRepository flux-system (https, token auth)
└── Kustomization flux-system        path ./clusters/homelab   SA kustomize-controller
    ├── flux-system/                 gotk-components.yaml + gotk-sync.yaml
    │                               (Deployments patched by clusters/homelab/kustomization.yaml)
    └── platform/                    Flux Kustomizations only — see "Stage ordering"
```

## Bootstrap

Needs a reachable cluster and a GitHub token with contents write on the
repository. `--token-auth` stores that token in the cluster; no deploy key is
created.

```sh
GITHUB_TOKEN=$(gh auth token) clusters/homelab/flux-system/bootstrap.sh
```

The script runs exactly this command (nothing weakening is added):

```sh
flux bootstrap github \
  --owner=smores56 \
  --repository=homelabs-platform \
  --branch=main \
  --path=clusters/homelab \
  --personal \
  --token-auth \
  --components-extra=source-watcher \
  --interval=10m
```

`--personal` selects a personal account; the repository stays private (bootstrap
default). The token is write-capable because bootstrap needs to push the
generated manifests; scope it to this repository only. The two
insecure kubeconfig flags appear nowhere in this repository:
`tests/policies/check.sh` fails if either is added to a manifest or script.

Bootstrap writes two files and commits them:

| File | Written by | Contents |
| --- | --- | --- |
| `flux-system/gotk-components.yaml` | `flux install --export` | CRDs, RBAC, controller Deployments |
| `flux-system/gotk-sync.yaml` | `flux bootstrap` | GitRepository + root Kustomization |

It does **not** write `clusters/homelab/kustomization.yaml` or
`flux-system/kustomization.yaml`; both are tracked here. The controller patches
live at `clusters/homelab/kustomization.yaml` precisely because bootstrap
regenerates the two files above and would otherwise drop patches stored beside
them.

### Why bootstrap re-renders `gotk-sync.yaml`

`flux bootstrap` regenerates the root Kustomization with no `serviceAccountName`.
Combined with the controller-wide `--default-service-account=default` every
Kustomization without a service account is impersonated as the `default` service
account in its own namespace, which has no RBAC. The root object applies
cluster-scoped resources (CRDs, RuntimeClass, ClusterRoleBindings), so it must
run as an identity that has those permissions — and only that object.

`bootstrap.sh` therefore re-renders the file from `--print-sync` after the
bootstrap commit and pushes a one-line follow-up commit pinning
`serviceAccountName: kustomize-controller`. `tests/policies/check.sh` diffs the
committed file against `bootstrap.sh --print-sync`, so the two cannot drift.

## Controller flags

Patches are JSON6902 appends to the `manager` container's `args`, in
`clusters/homelab/flux-system/gotk-patches/`. Appending keeps the upstream args
that carry telemetry and leader-election wiring.

| Controller | Version | Flags added |
| --- | --- | --- |
| `kustomize-controller` | v1.9.3 | `--feature-gates=ObjectLevelWorkloadIdentity=true`, `--no-cross-namespace-refs=true`, `--no-remote-bases=true`, `--default-service-account=default`, `--default-decryption-service-account=default`, `--default-kubeconfig-service-account=default` |
| `helm-controller` | v1.6.2 | `--feature-gates=ObjectLevelWorkloadIdentity=true`, `--no-cross-namespace-refs=true`, `--default-service-account=default`, `--default-kubeconfig-service-account=default` |
| `notification-controller` | v1.9.2 | `--feature-gates=ObjectLevelWorkloadIdentity=true`, `--no-cross-namespace-refs=true`, `--default-service-account=default` |
| `source-controller` | v1.9.3 | `--feature-gates=ObjectLevelWorkloadIdentity=true`, `--default-service-account=default` |
| `source-watcher` | v2.2.2 | `--no-cross-namespace-refs=true` |

Versions are the ones shipped by Flux 2.9.2 (`image:` lines in
`gotk-components.yaml`).

Flux 2.9 makes object-identity default service accounts opt-in behind
`--feature-gates=ObjectLevelWorkloadIdentity=true`. Without it, every controller
that receives a `--default-*-service-account` flag exits at startup with
`cannot set default service accounts when the feature gate ... is not enabled`.
Flux 2.10 retires the gate and enables the behavior unconditionally, so the
flag is harmless there (it only logs a retirement message).

Two entries differ from the obvious "same flags everywhere" reading, because the
binaries do:

- `source-controller` registers no ACL options at this version (`main.go` binds
  `artifactOptions`, `clientOptions`, log/leader-election/rate-limiter/watch/
  jitter/token-cache flags and `--default-service-account` — no
  `--no-cross-namespace-refs`). Passing it would crash the controller with
  `flag provided but not defined`.
- `source-watcher` binds the ACL options (v2.2.2 `cmd/main.go`)
  but no `--default-service-account`.

`tests/policies/check.sh` asserts the rendered `args` for all five controllers
against this table and fails on an added or missing flag, so the table and the
manifests cannot diverge.

Not set on purpose: `--insecure-kubeconfig-exec`, `--insecure-kubeconfig-tls`
and `--sops-age-secret`. The first two let a Kustomization run an arbitrary
command or skip TLS verification to obtain a kubeconfig. The third would give
every namespace one global age key; the per-namespace `decryption.secretRef`
contract in `docs/tenancy.md` keeps keys apart.

Image automation (`image-reflector-controller`, `image-automation-controller`) is
not installed. If it is added, both need `--no-cross-namespace-refs=true` and the
same digest pinning review as the rest of the tenant surface.

## Root namespace labels

The bootstrap manifests create `flux-system` without tenancy labels. The
`flux-system-namespace.json` patch adds `homelabs.dev/tier=platform`,
`homelabs.dev/tenant=platform`, `homelabs.dev/managed-by=platform` and
`pod-security.kubernetes.io/enforce=baseline` (audit/warn `restricted`), so the
policy bindings treat it like the rest of the platform tier.

## Stage ordering

`clusters/homelab/platform/kustomization.yaml` applies thirteen Flux Kustomizations;
each one's `dependsOn` is asserted by `tests/policies/check.sh`.

```text
platform-namespaces   namespaces + RuntimeClass gvisor
└── platform-rbac     tenant identities, Roles/Bindings, data SAs
    └── platform-policies   ValidatingAdmissionPolicies + bindings
platform-network      NetworkPolicies            (dependsOn namespaces)
platform-secrets      sops contract ConfigMap    (dependsOn namespaces, policies)
    └── platform-secrets-eso   ESO objects, suspended
platform-broker       broker Deployment/Service  (dependsOn namespaces, rbac,
                      policies, network, secrets)
platform-storage      StorageClass + static PVs  (dependsOn namespaces)
platform-backup       repository mirror CronJob/PVC (dependsOn namespaces,
                      policies)
platform-digest       cluster digest CronJob + PV/PVC (dependsOn namespaces,
                      policies, storage, observability)
platform-data-apps    Immich, Navidrome, Calibre, Graphiti
                      (dependsOn policies, network, storage)
platform-ingress      cloudflared + routing contract (dependsOn namespaces, network)
platform-observability
platform-tenants      GitRepository + tenant Kustomization
                      (dependsOn policies, network, secrets)
```

Every stage sets `serviceAccountName: kustomize-controller`, because it applies
cluster-scoped objects and the controller-wide default
(`--default-service-account=default`) has no RBAC.

## Regenerating the install manifests

```sh
flux install --export --components-extra=source-watcher \
  > clusters/homelab/flux-system/gotk-components.yaml
```

`scripts/kustomize-build-all.sh` and the `kubeconform` job validate the result,
and `tests/policies/check.sh` re-checks the patched controller args. The file is
excluded from `yamllint` because it must stay byte-identical to upstream output.
