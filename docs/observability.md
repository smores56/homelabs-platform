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

| Tenant | Written by | Content |
| --- | --- | --- |
| `raw` | Vector → Loki directly (`tenant_id: raw`) | full container log line for every non-data namespace |
| `data` | Vector → `loki-gateway` (bearer token) | allowlisted classification only |

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

`loki-gateway` is the only way into the `data` tenant. It is a stdlib Python
proxy (`clusters/homelab/observability/loki-gateway/gateway.py`) that:

- requires `Authorization: Bearer <token>` and compares it in constant time;
- proxies read paths only (`/loki/api/v1/{query,query_range,labels,label/*/values,series,index/stats,patterns,format_query}`, GET and POST);
- rejects push, delete and flush with 403, so it cannot be used to plant logs;
- sends a fresh header set upstream: `X-Scope-OrgID: data` is always set and the
  client's `Authorization`, `Cookie`, `X-Forwarded-For` and any client-sent
  `X-Scope-OrgID` are dropped. The token, not the header, chooses the tenant;
- refuses to start when `LOKI_GATEWAY_TOKEN` is unset (fail closed).

The token is an ESO `Password` generator (`loki-gateway/eso/`), materialised as
Secret `loki-gateway-token` (key `password`) in `observability`, consumed by both
Vector and the gateway. It is never in git, never in CI, and never given to an
agent.

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

The identity is the narrowest shape that can answer those questions: a
ClusterRole with `get/list/watch` on `pods` and `get/list` on
`deployments`/`statefulsets`, bound by **one RoleBinding per data namespace**
(`immich`, `navidrome`, `calibre`, `graphiti`; `DATA_NAMESPACES` in the
Deployment names the same four). A
ClusterRoleBinding would read every namespace; there is deliberately none, and
`tests/observability/check.sh` fails if one appears. The binding lives at
`clusters/homelab/observability/rbac-data.yaml` rather than beside the exporter
because the exporter's sub-kustomization sets `namespace: observability`, and
kustomize's namespace transformer would rewrite the bindings out of the data
namespaces.

The exporter is human-owned (`CODEOWNERS`): nanobot reads its metrics through the
broker or Prometheus and never runs or edits it. Prometheus scrapes it by static
target and also picks up pods annotated `prometheus.io/scrape: "true"`.

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
tests/policies/check.sh               # static policy stack (357 assertions)
scripts/kustomize-build-all.sh        # every clusters/** kustomization
nix run nixpkgs#kubeconform -- -strict -ignore-missing-schemas /tmp/manifests/*.yaml
```

Recorded 2026-11-11 (agent VM, Vector 0.61.0, Loki 3.7.9, Python 3.13, k3s
schema v1.34):

- `tests/observability/check.sh` → `observability: 29 passed, 0 failed`
  (6 `vector test` cases, 29 Python unit tests);
- `vector validate` and `loki -verify-config` both accept the shipped configs;
- `promtool check config` accepts the Prometheus config;
- `tests/policies/check.sh` → `static: 357 passed, 0 failed`;
- `tests/policies/check.sh --runtime` → `static+runtime: 457 passed, 0 failed`;
- `kustomize build` over every `clusters/**` tree and `kubeconform -strict`
  → `237 resources found ... Valid: 175, Invalid: 0`.

CI runs the `observability` job with Vector installed from the release tarball
whose SHA-256 is pinned in `scripts/install-ci-tools.sh`.

## What still needs the live cluster

| Not covered statically | Why | Where |
| --- | --- | --- |
| `kubernetes_logs` reading `/var/log/pods` and resolving pod/namespace metadata | needs a kubelet and the node's log directory | first cluster bring-up: `kubectl -n observability logs ds/vector` |
| Loki accepting a batch under each tenant | needs the server | `curl` with `X-Scope-OrgID: raw` and the gateway with the token |
| The planted-line end-to-end check | needs a running Immich | write the fixture line through a data app, then query `{tier="data"}` and confirm no filename/album |
| NetworkPolicy enforcement on the gateway path | needs a CNI | broker pod → gateway succeeds on 3100; tenant-apps pod → gateway fails |
| ESO materialising `loki-gateway-token` | needs the ESO operator | `docs/manual-steps.md` step 9 |
| Image digest pinning | needs a registry lookup | `docs/manual-steps.md` step 10 |
| PVC binding on `local-path` | needs the k3s provisioner | `kubectl -n observability get pvc` |
