# Observability

Prometheus, Loki, Vector and the data-plane helpers run in the `observability`
namespace (tier `observability`, PSA `privileged`). The stage is
`platform-observability` in the Flux chain, ordered after `platform-policies`,
so no workload is created before the admission and network guards exist. The
Externally generated secrets live in a second, suspended stage
(`platform-observability-secrets`) for the same reason as `platform-secrets-eso`:
the operator's CRDs are not shipped by this repository.

## Components

| Component | Image (pinned) | Purpose | Requests | Limits |
| --- | --- | --- | --- | --- |
| Prometheus | `prom/prometheus:v3.16.0` | metrics, 7d / 8GB retention, local PVC | 100m / 384Mi | 1 / 1200Mi |
| Loki | `grafana/loki:3.7.9` | single-binary log store, 168h retention, `auth_enabled: true` | 100m / 256Mi | 1 / 1024Mi |
| Vector | `timberio/vector:0.61.0-debian` | one DaemonSet, two sinks | 50m / 64Mi | 500m / 256Mi |
| status-exporter | `python:3.13.12-slim` + `exporter.py` | read-only ready/restarts/versions for `data` | 10m / 32Mi | 100m / 96Mi |
| loki-gateway | `python:3.13.12-slim` + `gateway.py` | bearer auth + forced `data` tenant | 10m / 32Mi | 100m / 96Mi |
| cluster-digest | `python:3.13.12-slim` + `digest_builder.py` | 15-minute bounded JSON digest (platform stage) | 10m / 32Mi | 100m / 128Mi |

Total requests are under 300m CPU and ~800Mi; limits sum to ~2.7Gi on the 15GB
guest. No component uses `hostNetwork`; only Vector reads host paths
(`/var/log/pods` read-only, `/var/lib/vector` for checkpoints), which is why the
namespace enforces `privileged` (see `docs/tenancy.md`). The hostPath directory is
created root-owned, so a pinned `busybox:1.37.0` init container hands it to uid
10001; Vector itself runs non-root with a read-only root filesystem.

**Prometheus, not VictoriaMetrics.** The brief allows either. Prometheus is one
binary with no operator, matches the `prometheus.io/scrape` annotation convention
that the rest of the platform already uses, and is what the status exporter and
any future exporter are written against. VictoriaMetrics would add
`vminsert`/`vmselect`/`vmstorage` processes to save memory at cluster scale; at
one node and a handful of targets that trade is not worth a second query
language and a second metric format. Revisit only if series count outgrows the
PVC.

Images are pinned by version tag; digest pinning is a manual step
(`docs/manual-steps.md`, step 10) because no registry credential or digest tool
runs in this repository's CI.

## Tenant model

Loki runs with `auth_enabled: true`, so every read and write must name a tenant.
Two tenants exist, created implicitly on first use:

| Tenant | Written by | Read by | Content |
| --- | --- | --- | --- |
| `raw` | Vector → Loki directly (`tenant_id: raw`) | nobody today | full container log line for every non-data namespace |
| `data` | Vector → Loki directly (`tenant_id: data`) | `loki-gateway` only (broker, digest) | allowlisted classification only |

Both sinks speak Loki's own write API with an explicit `tenant_id`, because that
is the API Vector implements; the gateway is a **read-only** proxy and 403s every
push (`/loki/api/v1/push`), so a sink pointed at it would discard the whole data
tenant while every freshness field still claimed success. Writes are scoped by
network instead: `loki-ingress` accepts 3100 from the Vector pod and the gateway
pod only, so no tenant, no broker and no Prometheus can set `X-Scope-OrgID`
themselves (`clusters/homelab/observability/network/loki-flows.yaml`).

Routing is on the **namespace** label `homelabs.dev/tier`, never a pod label: a
namespace is where the operator sets the tier, and a pod must not be able to
opt itself out of the allowlist. A namespace without the label (kube-system,
flux-system, default) ships `raw`.

The allowlist (`clusters/homelab/observability/vector/data_allowlist.vrl`) emits
exactly these fields, all optional except `namespace` and `timestamp`:

| Field | Source keys, first present wins |
| --- | --- |
| `level` | `level`, `severity`, `lvl` |
| `component` | `component`, `context`, `module`, `logger` |
| `error_code` | `error_code`, `err.code`, `code` |
| `status` | `status`, `statusCode`, `res.statusCode` |
| `namespace` | `.kubernetes.pod_namespace` (from the API, not the line) |
| `timestamp` | event timestamp |

The event object is rebuilt from scratch, so the message body is not merely
deleted: it is never copied. Non-JSON lines (postgres, redis) contribute a leading
level token and nothing else. Labels are deliberately minimal — `namespace`,
`tier`, `app`, `container`, `stream` for `raw`; `namespace`, `tier` for `data` —
because high-cardinality labels (pod name, path) make Loki unusable and would
themselves leak data. The line for a `data` event is compact JSON of the six
fields; `remove_timestamp: false` keeps the timestamp inside the line as well as
in the entry header.

## Loki gateway and the broker contract

`loki-gateway` is the only way *into* the `data` tenant for a reader. It is a
stdlib Python proxy (`clusters/homelab/observability/loki-gateway/gateway.py`)
that:

- requires `Authorization: Bearer <token>` and compares it in constant time;
- proxies read paths only (`/loki/api/v1/{query,query_range,labels,label/*/values,series,index/stats,patterns,format_query}`, GET and POST);
- rejects push, delete and flush with 403, so it cannot be used to plant logs;
- sends a fresh header set upstream: `X-Scope-OrgID: data` is always set and the
  client's `Authorization`, `Cookie`, `X-Forwarded-For` and any client-sent
  `X-Scope-OrgID` are dropped. The token, not the header, chooses the tenant;
- refuses to start when `LOKI_GATEWAY_TOKEN` is unset (fail closed).

The token is an ESO `Password` generator (`loki-gateway/eso/`), materialised as
Secret `loki-gateway-token` (key `password`) in `observability` and read by the
gateway only (Vector writes with a tenant header, so it needs no credential and
mounts no Secret). It is never in git, never in CI, and never given to an agent.

### Broker logical host `loki`

The broker (namespace `broker`, owned by the egress policy) is the only caller
allowed by NetworkPolicy (`network/loki-gateway-from-broker.yaml` and
`broker-to-loki-gateway.yaml`). The contract nanobot uses:

| | Value |
| --- | --- |
| Logical host | `loki` |
| Upstream URL | `http://loki-gateway.observability.svc.cluster.local:3100` |
| Method + paths | `GET`/`POST` `/loki/api/v1/query_range`, `/query`, `/labels`, `/label/<name>/values`, `/series` |
| Broker-injected header | `Authorization: Bearer <Secret observability/loki-gateway-token, key password>` |
| Tenant | fixed by the gateway: `X-Scope-OrgID: data` |
| Agent-supplied auth | stripped by the broker; the agent never holds the token |
| Denied | `/loki/api/v1/push`, `/delete`, `/flush`, everything outside the read paths |

Example request the broker may emit (from inside the `broker` namespace):

```sh
curl -sS -H "Authorization: Bearer $TOKEN" \
  --get "http://loki-gateway.observability.svc.cluster.local:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={tier="data"} | json | level="error"' \
  --data-urlencode 'limit=50'
```

The agent-facing contract is the logical host name and the query string only. The
broker maps `loki` → the URL above, attaches the token from its secret store and
drops any credential the agent tried to send. The broker does not enforce the
path list (it is not an action authorizer); the gateway's read-path check is the
policy, and its 403 is what the agent sees. Because the tenant header is
injected after authentication, an agent with a broker token can read `data`
classification and cannot read `raw`.

### Broker logical host `status`

The status exporter is reached the same way — through the broker, never with the
Kubernetes API:

| | Value |
| --- | --- |
| Logical host | `status` |
| Upstream URL | `http://status-exporter.observability.svc.cluster.local:9101/metrics` |
| Method | `GET` only |
| Broker-injected header | none (the endpoint is metrics text, not a credential) |
| Agent-supplied auth | stripped; the agent holds no Kubernetes token |
| Denied | everything except `/metrics`; the pod's RBAC and NetworkPolicy do not expose the API |

`network/status-exporter-from-broker.yaml` and
`network/broker-to-status-exporter.yaml` open exactly that one port from the
broker; the exporter's service account token is never mounted into an agent pod.

## Status exporter

nanobot has no `get pods` in a data namespace, and neither does any other agent
identity. `status-exporter` is the sanctioned view:

| Metric | Meaning |
| --- | --- |
| `homelabs_data_pod_ready{namespace,pod}` | 1 when every container is ready |
| `homelabs_data_pod_restart_count{namespace,pod}` | sum of container restart counts |
| `homelabs_data_workload_ready{namespace,kind,name}` | 1 when desired replicas are ready |
| `homelabs_data_workload_image{namespace,kind,name,image}` | 1 per image (version) |
| `homelabs_data_status_exporter_last_scrape_success` | 1 after a successful API read |
| `homelabs_data_status_exporter_up` | 1 while the exporter serves |
| `homelabs_cluster_node_ready{node}` | 1 when the node's `Ready` condition is True |

The identity is the narrowest shape that can answer those questions, and it is
split in two because a ClusterRoleBinding grants **every** rule of the role it
names:

| Role | Rules | Bound by | Effect |
| --- | --- | --- | --- |
| `observability-status-exporter` | `get/list/watch pods`, `get/list deployments,statefulsets` | one RoleBinding per data namespace (`immich`, `navidrome`, `calibre`, `graphiti`; `DATA_NAMESPACES` in the Deployment names the same four) | pods only in those four namespaces |
| `observability-status-exporter-nodes` | `get/list nodes` | one ClusterRoleBinding (`status-exporter/clusterrolebinding.yaml`) | node status cluster-wide; `nodes` is cluster-scoped, so only a ClusterRoleBinding can grant it |

Binding the workload role cluster-wide would hand `pods` out everywhere, which is
why `nodes` lives in its own role. `tests/observability/check.sh` renders the
tree and fails if the ClusterRoleBinding names the workload role;
`tests/policies/check.sh --runtime` proves the outcome with `kubectl auth can-i`
(`list nodes` yes, `get pods` in `immich` yes, `get pods` in `kube-system`,
`observability` and `tenant-apps` no). The RoleBindings live at
`clusters/homelab/observability/rbac-data.yaml` rather than beside the exporter
because the exporter's sub-kustomization sets `namespace: observability`, and
kustomize's namespace transformer would rewrite the bindings out of the data
namespaces.

The exporter is human-owned (`CODEOWNERS`): nanobot reads its metrics through the
broker or Prometheus and never runs or edits it. Prometheus scrapes it by static
target and also picks up pods annotated `prometheus.io/scrape: "true"`.

## Cluster digest

`clusters/homelab/platform/digest/` is the one-way hand-off to Kanbei
(smores56/sdlc#595). A CronJob every 15 minutes reads the two sanctioned views —
the status exporter's metrics and the `data` tenant through `loki-gateway` — and
writes one bounded JSON document to the cluster-digest share. The Flux stage is
`platform-digest`, after `platform-storage` and `platform-observability`
(the token Secret and both sources) and after `platform-policies`.

The objects live in `observability` because a Secret reference never crosses a
namespace and the ESO-generated `loki-gateway-token` lives there; every label is
`homelabs.dev/tier: platform` because the code is human-owned and no agent
supplies a byte of the input.

Document shape (`schema_version` `"1"`):

| Key | Content |
| --- | --- |
| `schema_version` | `"1"`; a reader must reject an unknown value |
| `generated_at` | UTC `YYYY-MM-DDTHH:MM:SSZ`, the host writer's cursor |
| `window` | Loki lookback, `15m` |
| `source_freshness.status_exporter` | `{ok, age_seconds, stale}`; age is `generated_at` minus the exporter's own `last_scrape_timestamp_seconds` sample, `null` when the sample is missing or the exporter was unreachable; `ok` also requires `last_scrape_success == 1` and `age_seconds <= 90` |
| `source_freshness.loki_gateway` | `{ok, age_seconds, stale}`; age is `generated_at` minus the newest stored data-tenant entry, `null` when the 15 m window held none; `stale` is that age missing or beyond the window |
| `cluster.nodes[]` | `{name, ready}` from `homelabs_cluster_node_ready` |
| `cluster.pods[]` | `{namespace, pod, ready, restarts}` |
| `cluster.workloads[]` | `{namespace, kind, name, ready, restarts, images}`; restarts are summed over pods whose name starts with the workload name |
| `nanobot_activity` | `observed_events`, `event_count`, `truncated`, `events[]` |
| `limits` | `max_bytes`, `max_events`, `max_files` the job enforces |

Each activity event carries **only** `logical_host`, `timestamp`, `level`,
`component`, `error_code` and `status`. `logical_host` is the Loki stream's
`namespace` label (which host emitted the line), never the pod or container. The
event object is rebuilt from that allowlist, so a message body, a prompt or a
tool payload cannot be copied — the same deny-by-construction rule as the Vector
allowlist, applied a second time. Values are stripped to printable ASCII and
64 characters, so a data-tenant writer cannot inflate the document through its
own `level` field. A line that is not JSON contributes nothing.

Ordering and bounds: keys are sorted, events are in a total order over the six
fields, and the encoded document is compact ASCII JSON with one trailing
newline. The 64 KiB budget is enforced by dropping the oldest events first and
setting `truncated`; if the mandatory sections alone exceed it the job exits 3
instead of writing. The last 96 snapshots (24 hours) are kept; `latest.json` is
always the newest.

### Share and host-side contract

The digest share is a virtiofs share of the `cluster` guest, one more entry in
the `dataShares` list of `modules/nixos/cluster-vm.nix` (lines 25–30 today;
add `"cluster-digest"` after `"graphiti"` at line 30 — that module is
nix-config's, not this repository's). The generated share is:

| | Value |
| --- | --- |
| Host path | `/var/lib/data/cluster-digest` |
| Guest path | `/mnt/data/cluster-digest` (mount of the `data-cluster-digest` virtiofs tag) |
| Static PV | `cluster-digest-share`, `data-local`, node affinity `cluster`, claimRef `observability/cluster-digest-data` |
| Writer | the CronJob, uid 1000 (the share's host owner through the guest translation) |
| Names it writes | `latest.json` plus `digest-<YYYYMMDDTHHMMSSZ>.json`, newest 96 retained |
| Atomicity | temp file in the same directory, fsync, `rename(2)`; a reader never sees a partial document |
| Max size | 65536 bytes; a larger mandatory document fails the run |
| Age boundary | **host-side**: the host writer watches `latest.json`, age-encrypts to Kanbei's published recipient and drops the ciphertext into `/var/lib/data/inbox` (T14). The cluster holds no age key and never writes the inbox |

The host writer must read only `latest.json` (snapshots are local debugging
history), keep `generated_at` as a cursor so a restart does not duplicate a
batch, and treat a missing `latest.json` as "not yet produced" rather than an
error. Ingest's 1 MiB batch cap (`modules/lib/ingest-vm.nix:23`) leaves the
64 KiB digest ample room for the envelope.

### Network and secrets

`cluster-digest-egress` allows DNS to `kube-system` and TCP 9101/3100 to
`status-exporter`/`loki-gateway` and nothing else: no `ipBlock`, so no
Cloudflare, GitHub or Neuralwatt egress. The same policy declares both
`policyTypes` with no ingress rules, which denies every inbound connection. The
two companion `*-from-digest` ingress policies are additive to the broker-only
ones in `observability/network/` (Kubernetes unions policies that select the
same pod). No Service selects the pod, so nothing can dial it by name, and no
RBAC object names its ServiceAccount.

### Network

The `observability` namespace is **default-deny in both directions**
(`observability/network/default-deny.yaml`). Every flow is its own file and the
full set is asserted by `tests/observability/check.sh`:

| File | Flow |
| --- | --- |
| `dns-egress.yaml` | every pod → `kube-system` 53 (UDP/TCP) |
| `loki-flows.yaml` | Vector → Loki 3100 (writes); gateway → Loki 3100 (reads); Loki ingress from exactly those two pods |
| `prometheus-flows.yaml` | Prometheus → `observability` 8686/9090/9101 and `flux-system` 8080 (the annotated targets); Vector ingress from Prometheus 8686; exporter ingress from Prometheus 9101 |
| `kubernetes-api-egress.yaml` | Vector, Prometheus, exporter → k3s service range `10.43.0.0/16` 443 |
| `loki-gateway-from-broker.yaml`, `broker-to-loki-gateway.yaml` | broker → gateway 3100 (both directions) |
| `status-exporter-from-broker.yaml`, `broker-to-status-exporter.yaml` | broker → exporter 9101 (both directions) |
| digest stage | digest → gateway/exporter, and the two `*-from-digest` ingress policies |

Nothing in the namespace has public egress, and no rule omits its destination.
Prometheus selects targets by annotation, which NetworkPolicy cannot express, so
its egress is a namespace+port allowlist: a new annotated workload means adding
its namespace and port; an unlisted one fails closed as a down target.

The only mounted Secret is `loki-gateway-token` (key `password`, projected as
the file `token`). It is the ESO `Password` generator's value, scoped by the
gateway to the `data` tenant; the pod never mounts a Kubernetes token
(`automountServiceAccountToken: false`), so it has no API access of its own.

## Postgres guidance for data apps

Every Postgres that stores data-tenant content must not log the content. The
fragment `clusters/homelab/data/immich/postgres-settings.yaml`
(`postgres-data-settings`, key `data-settings.conf`) sets:

```
log_error_verbosity = terse
log_min_error_statement = panic
log_statement = 'none'
```

`terse` removes the DETAIL/HINT lines that quote the failing value (an album
name, a filename, an email), and `panic` stops the server from echoing the
offending statement, which would carry the same content.

Consumption, in order of preference:

1. **Server flags** — works on any `postgres` image, including Immich's
   `ghcr.io/immich-app/postgres` and Graphiti's own database image:

   ```yaml
   command: ["postgres", "-c", "log_error_verbosity=terse",
             "-c", "log_min_error_statement=panic", "-c", "log_statement=none"]
   ```

   If the image pins its own `command`, mount the ConfigMap key at
   `/etc/postgresql/conf.d/10-homelabs-data.conf` and add
   `include_dir = '/etc/postgresql/conf.d'` to the image's `postgresql.conf`, or
   pass `-c config_file=/etc/postgresql/data-settings.conf` with the key mounted
   at that path.
2. **`POSTGRES_INITDB_ARGS`** — does not cover these settings; do not use it as
   the mechanism.

Immich is the only data app with a Postgres, so the fragment ships inside
`clusters/homelab/data/immich/` and is applied with that app in the `immich`
namespace. A future data app that adds one copies the fragment into its own
namespace: a ConfigMap cannot be shared across namespaces, and the data tier
deliberately has no namespace that holds only shared configuration.

## Verification

```sh
tests/observability/check.sh          # vector test + validate, drift, unit tests
tests/policies/check.sh               # static policy stack (364 assertions)
tests/digest/check.sh                 # digest builder fixtures + rendered contract
scripts/kustomize-build-all.sh        # every clusters/** kustomization
nix run nixpkgs#kubeconform -- -strict -ignore-missing-schemas /tmp/manifests/*.yaml
```

Recorded 2026-11-11 (agent VM, Vector 0.61.0, Loki 3.7.9, Python 3.13, k3s
schema v1.34):

- `tests/observability/check.sh` → `observability: 51 passed, 0 failed`
  (6 `vector test` cases, 35 Python unit tests, the rendered default-deny and
  allowlist set from `observability/network/`);
- `vector validate` and `loki -verify-config` both accept the shipped configs;
- `promtool check config` accepts the Prometheus config;
- `tests/policies/check.sh` → `static: 364 passed, 0 failed`;
- `tests/policies/check.sh --runtime` → `static+runtime: 477 passed, 0 failed`
  (throwaway etcd + kube-apiserver, kube-apiserver v1.36.2), including the
  exporter `can-i` probes: `list nodes` yes, `get pods` in `immich` yes,
  `get pods` in `kube-system`/`observability`/`tenant-apps` no;
- `tests/digest/check.sh` → `digest: 63 passed, 0 failed`;
- `kustomize build` over every `clusters/**` tree and `kubeconform -strict`
  → `409 resources found ... Valid: 331, Invalid: 0` (78 CRDs skipped).

CI runs the `observability` job with Vector installed from the release tarball
whose SHA-256 is pinned in `scripts/install-ci-tools.sh`.

## What still needs the live cluster

| Not covered statically | Why | Where |
| --- | --- | --- |
| `kubernetes_logs` reading `/var/log/pods` and resolving pod/namespace metadata | needs a kubelet and the node's log directory | first cluster bring-up: `kubectl -n observability logs ds/vector` |
| Loki accepting a batch under each tenant | needs the server | `curl` a push with `X-Scope-OrgID: raw` and one with `X-Scope-OrgID: data`; then a read through the gateway with the token |
| The Vector → Loki data-tenant write (the sink names `tenant_id: data` itself) | needs Loki and a log-producing pod | `kubectl -n observability logs ds/vector`, then `{tier="data"}` through the gateway; `source_freshness.loki_gateway.age_seconds` must be a real number |
| The planted-line end-to-end check | needs a running Immich | write the fixture line through a data app, then query `{tier="data"}` and confirm no filename/album |
| NetworkPolicy enforcement (default-deny, gateway 3100, Loki push 3100) | needs a CNI | broker pod → gateway succeeds on 3100; tenant-apps pod → gateway and → Loki fail; only Vector may push to Loki |
| ESO materialising `loki-gateway-token` | needs the ESO operator | `docs/manual-steps.md` step 9 |
| Image digest pinning | needs a registry lookup | `docs/manual-steps.md` step 10 |
| PVC binding on `local-path` | needs the provisioner `platform-storage` reconciles (Rancher v0.0.31, digest-pinned) | `kubectl -n observability get pvc` |
| The digest writing through the share | needs the `cluster-digest` virtiofs share and the static PV | `docs/manual-steps.md` step 23 |
| Digest egress and the absent reverse path | needs a CNI | `docs/manual-steps.md` step 23, steps 3 and 6 |
| The host writer age-encrypting a digest into the inbox | needs the T14 writer and Kanbei's recipient | `docs/manual-steps.md` step 23, step 5 |
