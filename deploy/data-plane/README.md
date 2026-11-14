# Data-plane deployment tree

Self-contained Kubernetes manifests for the Nix-free data plane's services
(feature #660). Nothing here is referenced by `clusters/**`, `platform/**` or an
admission policy, so `scripts/kustomize-build-all.sh` and Flux are unaffected.
The tree is applied deliberately, by an operator, or wired into Flux later with
its own Kustomization.

The tree is static: it creates no Secret, holds no credential and mounts no
plaintext value. Credential delivery and the host-side ZFS snapshotter are
outside this directory and are named under [Assumptions](#assumptions).

## Layout

| Path | What it is |
| --- | --- |
| `base/` | namespace, service accounts, RBAC, every service workload |
| `config/` | source catalog, recovery-group inventory, one relay schema per source |
| `policies/` | default-deny, DNS, every explicit service-to-service flow, image policy |
| `local/` | kind/minikube overlay; changes only the PVC StorageClass |
| `images.yaml` | required-image contract, kept in sync with `base/kustomization.yaml` |
| `tests/check.sh` | render with kustomize and validate with kubeconform |

## What exists

`kustomize build deploy/data-plane/base` renders 41 objects in `data-plane`.

| Service | Objects | Port | State | Identity / RBAC | Network |
| --- | --- | --- | --- | --- | --- |
| `event-service` | Deployment (1, Recreate), Service, PVC 2Gi | 8080 | SQLite event log | `event-service` SA, no token, no Role | ingress from write-broker, confirmation and backup only; egress DNS only |
| `write-broker` | Deployment (1, Recreate), Service | 8080 | stateless; intents and effects in event-service | `write-broker` SA, no token, no Role | ingress from `tenant-apps/nanobot`, `broker/broker` and confirmation; egress event-service only; no public egress |
| `confirmation-surface` | Deployment (1, Recreate), Service | 8080 | stateless; approvals in event-service | `confirmation-surface` SA, no token, no Role | ingress from `ingress/cloudflared`; egress event-service + write-broker |
| `backup-controller` | Deployment (1, Recreate), PVC 5Gi | 8080 probe only | generation manifests + snapshot requests | `backup-controller` SA, one-hour projected token (0400), namespaced Role (PVC/pod read only) | egress event-service and the k3s API; no ingress |
| `collector-brave` | Deployment (1, Recreate) | 8080 pod-local | collector + relay in one pod | `collector-relay` SA, no token, no Role | egress `broker/brave-proxy` only; no public egress |
| `collector-finance` | Deployment (1, Recreate) | 8080 pod-local | same | same | egress `broker/finance-proxy` only |
| `collector-tuta` | Deployment (1, Recreate) | 8080 pod-local | same | same | egress `broker/tuta-proxy` only |
| `collector-immich` | Deployment (1, Recreate) | 8080 pod-local | same | same | egress `broker/immich-proxy` only |
| `recovery-portal` | Deployment (1, Recreate), Service | 8080 | reads the manifest claim | `recovery-portal` SA, no token, no Role | ingress `ingress/cloudflared`; no egress (a download is a redirect to a pre-signed URL) |
| `file-memory` | **no object, by decision** | — | library | — | — |

### `file-memory` is a library, not a service

`libs/data_plane/memory/file_memory.py` is a file-backed record store with no
listener, no socket and no durable state outside its directory. Each profile
mounts its own tree (`/srv/memory/local` or `/srv/memory/external`) from the
workload that owns it, and the `memory-local` / `memory-external` recovery groups
back those datasets up. A Deployment here would add a second writer to a
single-writer store for no capability. The decision is recorded in
`base/kustomization.yaml` as well as here.

## Required images and digests

The registry is a placeholder. One image serves every service; the Deployment's
`args` selects the subcommand. No image exists there yet, and no tag is valid.

| Service | Subcommand | Image reference | Digest variable |
| --- | --- | --- | --- |
| event-service | `event` | `ghcr.io/smores56/homelabs-platform/data-plane@sha256:<64 hex>` | `DATA_PLANE_IMAGE_DIGEST` |
| write-broker | `broker` | same image | `DATA_PLANE_IMAGE_DIGEST` |
| confirmation-surface | `confirmation` | same image | `DATA_PLANE_IMAGE_DIGEST` |
| backup-controller | `backup` | same image | `DATA_PLANE_IMAGE_DIGEST` |
| collector-relay (4) | `collector` | same image | `DATA_PLANE_IMAGE_DIGEST` |
| recovery-portal | `portal` | same image | `DATA_PLANE_IMAGE_DIGEST` |

The image currently ends in the all-zero placeholder digest
`sha256:0000…0000`. Replace it with the `@sha256` value printed by the publish
workflow before any apply. `tests/check.sh` rejects a tag, a floating reference
and `latest`.

The image's entrypoint is `python -m apps.data_plane.run`; the Deployment `args`
is the service name. `apps/data_plane/run.py` implements `event`, `broker`,
`relay` and `backup` today; `collector`, `confirmation` and `portal` are the
forward contracts this base expects (see [Assumptions](#assumptions)). The
fixture broker keeps a local SQLite journal, but the deployment contract here is
the event-service-backed broker: the base sets `EVENT_SERVICE_URL` and no local
claim.

- every service answers `GET /healthz` on `DATA_PLANE_PORT`; the body is
  `{"status": "ok", "service": ...}` only, never an epoch, a group or a
  destination, because the probe is unauthenticated;
- `DATA_PLANE_HOST`, `DATA_PLANE_PORT`, `DATA_PLANE_DB`, `DATA_PLANE_CATALOG`,
  `DATA_PLANE_SCHEMA`, `DATA_PLANE_MANIFEST_DIR`, `DATA_PLANE_GROUPS` and
  `DATA_PLANE_DESTINATIONS` are the configuration surface;
- `collector-relay` selects its source with `SOURCE_ID`, its schema with
  `DATA_PLANE_SCHEMA` and its only external destination with `SOURCE_PROXY_URL`;
- `DATA_PLANE_SECRET_FILE` names a path, not a value. The base mounts nothing
  there; a profile overlay supplies the operator-provisioned Secret. Each relay
  gets its own path (`/run/secrets/relay/<source>/token`); no relay secret is
  shared, and no relay holds the event-service token.

`images.yaml` repeats the same one-entry contract. kustomize v5 cannot include an
images-only file, so `base/kustomization.yaml` carries the transformer and
`tests/check.sh` fails if the two blocks drift.

## Apply and verification sequence

This change was rendered and validated locally; no cluster was contacted and
nothing was applied.

```sh
# 1. Render and validate. Missing kustomize/kubeconform skips; a bad render fails.
nix-shell -p kustomize kubeconform --run "deploy/data-plane/tests/check.sh"
kustomize build deploy/data-plane/base > /tmp/data-plane.yaml

# 2. Replace the digest placeholder in base/kustomization.yaml and images.yaml,
#    then repeat step 1. The script fails if the lists drift or the image is not
#    digest-pinned. A profile overlay also mounts the credential Secrets at the
#    DATA_PLANE_SECRET_FILE paths; the base mounts none.

# 3. Client-side dry run; no server and no admission call.
kubectl apply --dry-run=client --validate=false -f /tmp/data-plane.yaml

# 4. Apply only when a live cluster is intended. Server-side apply avoids the
#    last-applied annotation and is safe to re-run.
kubectl apply --server-side -f /tmp/data-plane.yaml
kubectl -n data-plane rollout status deployment/event-service --timeout=120s
kubectl -n data-plane rollout status deployment/write-broker --timeout=120s
kubectl -n data-plane rollout status deployment/confirmation-surface --timeout=120s
kubectl -n data-plane rollout status deployment/backup-controller --timeout=120s
kubectl -n data-plane rollout status deployment/recovery-portal --timeout=120s
for source in brave finance tuta immich; do
  kubectl -n data-plane rollout status "deployment/collector-$source" --timeout=120s
done
```

Verification, in order:

```sh
# Objects and PSA.
kubectl -n data-plane get deploy,svc,pvc,sa,role,rolebinding,networkpolicy,configmap
kubectl get namespace data-plane -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}{"\n"}'
# must print: restricted

# Token posture: every Deployment must show false, including backup-controller.
kubectl -n data-plane get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.automountServiceAccountToken}{"\n"}{end}'

# RBAC ceiling. Yes: PVC/pods read in data-plane. No: jobs, secrets, exec, logs,
# nodes, other namespaces.
kubectl auth can-i --list --namespace data-plane --as=system:serviceaccount:data-plane:backup-controller
kubectl auth can-i create jobs --namespace data-plane --as=system:serviceaccount:data-plane:backup-controller   # must print: no
kubectl auth can-i get pods/log --namespace data-plane --as=system:serviceaccount:data-plane:backup-controller   # must print: no
kubectl auth can-i get secrets --namespace data-plane --as=system:serviceaccount:data-plane:backup-controller
kubectl auth can-i list pods --all-namespaces --as=system:serviceaccount:data-plane:backup-controller

# Health, from the machine running kubectl.
kubectl -n data-plane port-forward svc/event-service 8080:8080 &
curl -fsS http://127.0.0.1:8080/healthz
kubectl -n data-plane port-forward svc/recovery-portal 8081:8080 &
curl -fsS http://127.0.0.1:8081/healthz

# Negative network checks. Use a probe image if the workload image has no curl.
# The first must time out: the agent has no event-service route.
kubectl -n tenant-apps exec deploy/nanobot -- \
  curl -sS --max-time 3 http://event-service.data-plane.svc.cluster.local:8080/healthz
# The second must succeed through the agent namespace rule.
kubectl -n broker exec deploy/broker -- \
  curl -sS --max-time 3 http://write-broker.data-plane.svc.cluster.local:8080/healthz
# The third must fail: collectors have no direct public egress.
kubectl -n data-plane exec deploy/collector-brave -- \
  curl -sS --max-time 3 https://example.com
```

## Assumptions

- **StorageClass.** The base uses `local-path`, the k3s default, by name only;
  no manifest contains a `hostPath`. `local/kustomization.yaml` switches the
  claims to `standard` for kind/minikube. The data tier's `data-local` class has
  no provisioner and is intentionally not used here.
- **Co-located `ReadWriteOnce` claim.** `backup-controller` writes and
  `recovery-portal` reads the same `backup-manifests` claim, the portal
  read-only. On single-node k3s this is automatic. A multi-node deployment needs
  a mirrored read-only copy, not a second writer.
- **Host-side snapshotter.** The controller writes a request file under
  `/var/lib/data-plane/manifests/requests` and reads the resulting generation
  manifest. A host unit owns ZFS, Restic and the repositories. That is why the
  pod has no `hostPath`, no privileged mode and no cluster-scoped RBAC.
- **Source proxies.** Each collector reaches `<source>-proxy` in the existing
  `broker` tier on 8443. The proxies are operator-supplied. Until one exists the
  egress rule matches nothing and the collector fails closed.
- **Collector ingestion.** A relay has no event-service route and no
  event-service token: the event log admits only write-broker,
  confirmation-surface and backup-controller. The relay's sink is the pod-local
  collector process today, so no flow is lost. When the sink becomes remote it
  needs an explicit policy and ingress rule reviewed on its own; this base does
  not widen the event log in anticipation.
- **API egress range.** `kubernetes-api-egress.yaml` names only the apiserver
  endpoint `10.43.0.1/32` on 443/6443, not the service range or a node address.
  A different cluster needs its own endpoint address. Only `backup-controller` is
  selected; it is the only pod with a token.
- **Image subcommands.** The published image must expose `event`, `broker`,
  `relay`, `backup`, `collector`, `confirmation` and `portal`. `run.py`
  implements the first four today; the remaining three are the forward contract
  this base depends on. Until they exist, those Deployments exit with a usage
  error rather than serving a half-configured endpoint.
- **Credential paths, not values.** No Secret object and no credential value is
  created. The base sets `DATA_PLANE_SECRET_FILE` for event-service,
  write-broker, confirmation-surface and the collectors, but mounts nothing at
  that path; a profile overlay adds the operator-provisioned Secret. Those
  processes fail closed until it exists. Source credentials stay with the
  collector runtime's LUKS-backed host files, and R2 access is supplied by the
  operator's restore tooling.
- **Runtime class.** The base is PSA `restricted` (non-root, read-only rootfs,
  dropped capabilities, RuntimeDefault seccomp). Kata/gVisor isolation for
  collectors is a runtime/profile overlay, not a manifest in this base.
- **NetworkPolicy implementation.** The rules rely on a CNI that enforces
  NetworkPolicy (kube-router on k3s). A CNI that ignores policies renders them
  but does not enforce them.
- **No live apply.** The manifests were rendered and schema-validated with
  `kustomize` 5.8.1 and `kubeconform` 0.7.0: 41 resources valid in the base and
  41 in the local overlay.
