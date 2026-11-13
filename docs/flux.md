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
  --components-extra=source-watcher,image-reflector-controller,image-automation-controller \
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
| `image-reflector-controller` | v1.2.3 | `--no-cross-namespace-refs=true` |
| `image-automation-controller` | v1.2.3 | `--no-cross-namespace-refs=true` |

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

`tests/policies/check.sh` asserts the rendered `args` for all seven controllers
against this table and fails on an added or missing flag, so the table and the
manifests cannot diverge.

Not set on purpose: `--insecure-kubeconfig-exec`, `--insecure-kubeconfig-tls`
and `--sops-age-secret`. The first two let a Kustomization run an arbitrary
command or skip TLS verification to obtain a kubeconfig. The third would give
every namespace one global age key; the per-namespace `decryption.secretRef`
contract in `docs/tenancy.md` keeps keys apart.

## Image automation

`image-reflector-controller` and `image-automation-controller` are part of the
install manifest (`--components-extra` above) and are patched with
`--no-cross-namespace-refs=true` like every other controller. Their objects live
in `clusters/homelab/flux-system/image-automation/`, which `flux bootstrap` does
not regenerate:

| Object | Namespace | Contract |
| --- | --- | --- |
| `ImageRepository nanobot` | flux-system | watches `ghcr.io/smores56/homelabs-platform/nanobot` |
| `ImagePolicy nanobot` | flux-system | `^build-(?P<run>[0-9]+)$` → numerical, ascending |
| `GitRepository homelabs-apps` | flux-system | SSH 443, `homelabs-apps-write` (SOPS-managed) |
| `ImageUpdateAutomation nanobot` | flux-system | Setters, `./`, pushes `main` |

`publish-nanobot.yml` tags every build `build-<run_number>`, so the policy picks
the highest run number and Flux commits the resolved digest to
`smores56/homelabs-apps`. The tenant side carries the marker:

```yaml
image: ghcr.io/smores56/homelabs-platform/nanobot@sha256:<digest>
  # {"$imagepolicy": "flux-system:nanobot"}
```

The write credential never enters git: it is a SOPS-encrypted deploy key turned
into the `homelabs-apps-write` Secret (docs/manual-steps.md, "nanobot image
automation"). Until it exists the GitRepository is NotReady and nothing is
pushed — fail closed, like every other credential here. The tenant's own
GitRepository keeps its read-only key; the two secrets are in different
namespaces and `--no-cross-namespace-refs` keeps them apart.

### Dispatch contract (`POST /self/build`)

agent-control triggers a build through the Actions API; it holds a platform
token with `Actions: write` on this repository and never passes it on.

```text
POST /repos/smores56/homelabs-platform/actions/workflows/publish-nanobot.yml/dispatches
{"ref": "main"}
```

- workflow file (dispatch id): `publish-nanobot.yml`
- trigger: `workflow_dispatch`, no inputs; `ref` is always `main`
- result: `build-<GITHUB_RUN_NUMBER>` pushed, digest in the run summary
- isolation: the dispatcher's token stays with the dispatcher; the job's
  `GITHUB_TOKEN` is scoped to that run (`packages: write`, `contents: read`),
  is never printed, and the tenant repository holds no credential for either
- retry: a second dispatch is a new run number, which the `numerical` policy
  orders above the previous one; duplicate builds are harmless

## Root namespace labels

The bootstrap manifests create `flux-system` without tenancy labels. The
`flux-system-namespace.json` patch adds `homelabs.dev/tier=platform`,
`homelabs.dev/tenant=platform`, `homelabs.dev/managed-by=platform` and
`pod-security.kubernetes.io/enforce=baseline` (audit/warn `restricted`), so the
policy bindings treat it like the rest of the platform tier.

## Stage ordering

`clusters/homelab/platform/kustomization.yaml` applies fourteen Flux Kustomizations;
each one's `dependsOn` is asserted by `tests/policies/check.sh`.

```text
platform-namespaces   namespaces + RuntimeClass gvisor
└── platform-rbac     tenant identities, Roles/Bindings, data SAs
    └── platform-policies   ValidatingAdmissionPolicies + bindings
platform-network      NetworkPolicies            (dependsOn namespaces)
platform-secrets      sops contract ConfigMap    (dependsOn namespaces, policies)
    └── platform-secrets-eso   ESO objects (needs the operator)
platform-broker       broker Deployment/Service  (dependsOn namespaces, rbac,
                      policies, network, secrets)
platform-webhook      inbound webhook receiver  (dependsOn namespaces,
                      network, policies, secrets)
platform-storage      StorageClass + static PVs  (dependsOn namespaces)
platform-backup       repository mirror CronJob/PVC (dependsOn namespaces,
                      policies)
platform-digest       cluster digest CronJob + PV/PVC (dependsOn namespaces,
                      policies, storage, observability)
platform-data-apps    Immich, Navidrome, Calibre, Graphiti
                      (dependsOn policies, network, storage)
platform-ingress      cloudflared + routing contract (dependsOn namespaces, network)
platform-observability
platform-nanobot-policy   nanobot-policy ConfigMap in tenant-apps
                      (dependsOn namespaces, policies)
platform-tenants      GitRepository + tenant Kustomization
                      (dependsOn policies, network, secrets)
```

Every stage sets `serviceAccountName: kustomize-controller`, because it applies
cluster-scoped objects and the controller-wide default
(`--default-service-account=default`) has no RBAC.

## Regenerating the install manifests

```sh
flux install --export \
  --components-extra=source-watcher,image-reflector-controller,image-automation-controller \
  > clusters/homelab/flux-system/gotk-components.yaml
```

`scripts/kustomize-build-all.sh` and the `kubeconform` job validate the result,
and `tests/policies/check.sh` re-checks the patched controller args. The file is
excluded from `yamllint` because it must stay byte-identical to upstream output.
