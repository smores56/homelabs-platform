# Negative tests

## fork PR cannot read the `cloudflare` environment secret

SDLC #576. Property under test: a pull request opened from a fork reaches no
secret, so an agent that can open a fork PR cannot exfiltrate the Cloudflare
token or run tofu against the real account.

### What runs automatically

Job `fork-pr-secret-boundary` in `.github/workflows/ci.yml`:

- Trigger: `pull_request` only (fork and same-repo PRs alike).
- Job-level `environment` is absent, so the job has no environment secrets.
- Step `fork PRs receive no secrets` asserts that `CLOUDFLARE_API_TOKEN` is
  unset in the job environment (`test -z "${CLOUDFLARE_API_TOKEN:-}"`). A fork
  PR passes only because the variable is never injected.
- Step `no pull_request workflow references a secret` runs
  `scripts/check-pr-secret-boundary.sh`, which statically asserts:
  - no workflow triggered by `pull_request` references `secrets.<NAME>` other
    than `secrets.GITHUB_TOKEN` (read-only on fork runs);
  - no workflow uses `pull_request_target`, which runs with base-repo secrets.

Both checks run on every PR, so they also guard the invariant over time: adding
`secrets.CLOUDFLARE_API_TOKEN` to a `pull_request` workflow fails CI on that PR.

Result of the static half: **PASS** locally on the bootstrap commits
(`scripts/check-pr-secret-boundary.sh` → `no pull_request workflow references
secrets beyond GITHUB_TOKEN`), and the same script is green in CI job
`action SHA pins`' sibling checks on run
[43024148131](https://github.com/smores56/homelabs-platform/actions/runs/43024148131)
(`ci`, push to `main`, all jobs success). Job `fork-pr-secret-boundary` itself
is skipped on pushes by its `if:` guard; it has not run yet because no pull
request exists on the repository (branch protection is not applied, see
`docs/runbook.md`). Its first PR run URL belongs in "Runtime result" below.

### What cannot run here

Opening a real fork PR requires a second GitHub identity: GitHub forbids
forking your own repository, and a private repo only accepts forks from
accounts with access. The machine user does not exist yet (see
`docs/manual-steps.md`, step 1), so the runtime half of the test is **manual**,
not executed.

### Manual runtime test

1. Complete `docs/manual-steps.md` steps 1 and 3 (machine user, environment
   secret).
2. As the machine user, fork `smores56/homelabs-platform` and open a branch
   whose `.github/workflows/probe.yml` contains:

   ```yaml
   name: probe
   on: pull_request
   permissions: read-all
   jobs:
     probe:
       runs-on: ubuntu-latest
       steps:
         - run: |
             echo "environment secret: ${CLOUDFLARE_API_TOKEN:-ABSENT}"
             echo "repo secret: ${OTHER_SECRET:-ABSENT}"
   ```

   This is expected to fail CI: the workflow is a `pull_request` workflow that
   references no secret by pattern, so the boundary check passes it, while the
   runtime probe prints `ABSENT` for both. A second variant that writes
   `secrets.CLOUDFLARE_API_TOKEN` into `env` must fail
   `fork-pr-secret-boundary` before it runs.
3. Record the run URL and the printed `ABSENT` lines under "Runtime result"
   below, then close the fork PR.

Approval gate: repo setting `fork-pr-contributor-approval` is intended to be
`all_external_contributors`; the bootstrap token could not apply it (HTTP 403,
`docs/runbook.md`). Until the owner applies it, GitHub's default applies: a
first-time contributor's workflow run on a private repo waits for owner
approval, and every fork run receives a read-only `GITHUB_TOKEN` regardless.

### Runtime result

Not executed: no second identity. Manual step recorded above; static half
passes.

## The platform policy stack fails closed

SDLC #578. Property under test: a tenant workload cannot reach the host, run
outside gVisor, pull a mutable image, mount the age key, adopt another app's
claim, or start a second reconciler with a different identity; a tenant identity
cannot read or mint credentials, edit RBAC, create cluster-scoped objects, or
touch data namespaces; and data workloads accept traffic only from the ingress
namespace on one port.

### What runs automatically (static, CI job `policy checks`)

`tests/policies/check.sh` runs with no cluster and no credentials on every push
and pull request. 277 assertions (recorded run, 2026-11-11):

| Family | Assertions |
| --- | --- |
| `kustomize build clusters/homelab` renders, every `clusters/` and `tests/` YAML file parses (`yq`) | 2 |
| Policy/binding pairing, names, `failurePolicy: Fail` | 55 |
| Each binding selects exactly the namespaces in `tests/policies/bindings.tsv`, resolved against the real namespace labels | 11 |
| Each fixture's expected policy really contains the CEL field the fixture violates | 25 |
| RBAC: one `data-workload` per data namespace, tenant identity, deny list, wildcard-free rules, no cluster-scoped objects | 10 |
| Rendered controller `args` match the reviewed flag table (`docs/flux.md`), gotk patch JSON, no insecure kubeconfig flags | 14 |
| NetworkPolicy shape: default-deny and DNS in every tier namespace, broker-only tenant egress, per-app ingress HTTP, exact per-namespace flow sets, ingress egress to the four Services and the edge | 26 |
| Storage: `data-local` class, PV paths/claimRefs/node affinity, one PVC per namespace, no `hostPath`, dump schedule/timezone/deadline/path, hostname-to-Service contract | 39 |
| Data workload shape: `data-workload` identity, `managed-by=platform`, uid 1000, limits, probes | 5 |
| Stage DAG (`dependsOn`), stage service accounts, apply order | 42 |
| `gotk-sync.yaml` equals `bootstrap.sh --print-sync`, bootstrap flags, tenant Kustomization pins | 13 |
| Fixture/expectation table completeness and `# expect:` headers | 35 |

The static half cannot evaluate CEL: it proves the expression *mentions* the
field and that the binding reaches the intended namespaces. Enforcement is the
runtime half.

### What runs on demand (runtime, real API server)

```sh
nix shell nixpkgs#etcd nixpkgs#kubernetes nixpkgs#openssl nixpkgs#kustomize \
  nixpkgs#yq-go nixpkgs#jq -c tests/policies/check.sh --runtime
```

`tests/policies/apiserver.sh` boots etcd plus `kube-apiserver` on loopback ports
(no kubelet, no container runtime, no root). The checker applies the real
`gotk-components.yaml` (for the Flux CRDs), then the seven platform stages it can
apply without a kubelet in order, then drives every case with
`kubectl --dry-run=server` and `kubectl auth can-i`.
71 assertions (recorded run, 2026-11-11, kube-apiserver v1.36.2, etcd 3.6.13):

- 7 stage applies (namespaces, rbac, policies, network, secrets, storage,
  tenants) plus the Flux component apply;
- 34 fixture admissions: 9 allowed, 25 denied, each denial matched to the policy
  named in `fixtures/expectations.tsv` by its denier string;
- 27 `can-i` probes from `tests/policies/rbac.tsv`, run as
  `system:serviceaccount:tenant-apps:tenant-apps-reconciler`;
- 2 live attempts over the API (create a Secret, create a Kustomization), both
  `forbidden`.

Result: `static+runtime: 474 passed, 0 failed`.

Two deliberate deviations from a production cluster, both so a failure is
attributable:

- PSA enforcement is relabelled to `privileged` on the bench after the real
  namespaces are applied. Otherwise PSA answers first for host-access fixtures and
  the test would pass for the wrong reason. The shipped labels stay `restricted`
  and are asserted statically (`bindings.tsv` selects on tier, and
  `namespaces.yaml` is reviewed YAML).
- `--disable-admission-plugins=ServiceAccount` and `--allow-privileged=true`: the
  bench runs no controller manager and has no privileged-container policy of its
  own. `--authorization-mode=RBAC` keeps the RBAC half real.

### What still needs a live cluster

| Not covered here | Why | Where it is exercised |
| --- | --- | --- |
| Flux reconciliation, `dependsOn` waits, prune behaviour | needs kustomize-controller | first real cluster bring-up (`docs/runbook.md`) |
| `RuntimeClass gvisor` actually starting under `runsc` | needs containerd + node | node bring-up; pod creation fails closed if the handler is missing |
| NetworkPolicy enforcement | needs a CNI; a bare API server only stores objects | cluster bring-up; the policies are shaped and cross-checked statically |
| PSA at `restricted` on tenant namespaces | relabelled on the bench (above) | cluster bring-up: create a hostPath pod in `tenant-apps`, expect a PSA violation |
| SOPS decryption with the real age key | key is human-held | `docs/runbook.md` step 1, then `flux -n tenant-apps get kustomization` |
| ESO `Password`/`ExternalSecret` materialising | needs the ESO operator CRDs | after `docs/runbook.md` step 3 resumes `platform-secrets-eso` |
| Digest-pinned image pulls | fixtures use a syntax-valid placeholder digest | cluster bring-up pulls the real digests |

### Runtime result

Executed on 2026-11-11 in the agent VM against the ephemeral control plane:
`tests/policies/check.sh --runtime` → `static+runtime: 474 passed, 0 failed`.
CI runs both halves as separate required jobs: `policy checks` (static, no
cluster) and `policy admission runtime` (boots etcd + kube-apiserver with
`tests/policies/apiserver.sh`, then drives every fixture and `rbac.tsv` row).
A vacuous CEL expression or a wrong RBAC grant therefore fails the pull request
that introduces it, not the next manual run.

## Data-tenant logs carry classification only

SDLC #579. Property under test: a planted Immich log line that names a file and
an album reaches Loki's `data` tenant as `level`, `component`, `error_code`,
`status`, `namespace` and `timestamp` and nothing else; a non-data namespace
ships raw; and the `data` tenant is unreachable without the gateway token.

### What runs automatically (CI job `observability pipeline`)

`tests/observability/check.sh` runs on every push and pull request with no
cluster and no credentials. 51 assertions (recorded run, 2026-11-11):

| Family | Assertions |
| --- | --- |
| `vector test` on `tests/vector/vector.yaml`: planted fixture loses body, text log keeps only a level token, JSON without classification keeps metadata only, namespace-label routing for data/tenant/unlabelled | 6 cases in 1 assertion |
| `vector validate --no-environment` on the shipped config, with the VRL staged where the container mounts it | 1 |
| Shipped-vs-test drift: `tenant_route` source, both filter conditions, both `file:` paths, the `vector-vrl` generator entry | 6 |
| The planted fixture really carries `file`, `albumName`, `originalPath`, and the unit-test input parses to the same JSON | 2 |
| Tenant separation and Loki: `tenant_id: raw` and `tenant_id: data`, the data sink targets Loki directly (never the read-only gateway), no gateway token or Secret on Vector, `tier=data` label, `auth_enabled`, 168h retention, telemetry off | 8 |
| Token contract: the gateway reads `loki-gateway-token`, ExternalSecret targets it, generator kind is `Password`, Vector holds no token and mounts no Secret | 5 |
| Status exporter RBAC: workload ClusterRole (pods + apps) and nodes-only ClusterRole exact, one ClusterRoleBinding naming the nodes role, RoleBindings per data namespace, subject `observability`, and the rendered tree's binding set | 11 |
| Network allowlists: default-deny both directions, exact policy set, DNS to `kube-system`, Loki ingress from Vector + gateway, Vector egress to Loki, gateway egress to Loki, Prometheus scrape ports and Flux controllers, broker paths kept, API egress range, no public egress, no destination-less rule | 14 |
| Postgres fragment sets `terse` + `panic`; every observability image is version/digest pinned | 2 |
| Python unit tests for `exporter.py` and `gateway.py` (including the cache timestamp that makes freshness real) | 35 tests in 1 assertion |

The deny-by-construction part is in the VRL itself: the event is rebuilt from an
allowlist, so a parser failure or an unknown log format cannot fall back to
shipping the line. The negative assertions on the planted fixture are
`!exists(.message)`, `!exists(.file)`, `!exists(.albumName)`,
`!exists(.originalPath)`, and `!contains(encode_json(.), "Vacation" | "IMG_1234" | "Alice")`.

Two properties are asserted statically rather than exercised: the routing
comparison (unit tests call the same `tenant_route` source the DaemonSet ships,
and the drift check fails if the two strings differ) and the gateway's tenant
header (unit tests prove the client's `X-Scope-OrgID` is replaced). The Vector
sink and the gateway are checked to agree on the direction of traffic: Vector
writes Loki's push API with `tenant_id: data`, and the gateway — which 403s
`/loki/api/v1/push` — is only ever a read path, so no test can pass with a sink
that the gateway would reject.

### What cannot run here

No kubelet, so `kubernetes_logs` never reads `/var/log/pods`; no Loki, so no
batch is actually accepted; no Immich, so the line is planted through a Vector
unit test rather than through a container log; no CNI, so the observability
default-deny and the broker/gateway/Loki allowlists are only shape-checked; no
ESO, so `loki-gateway-token` does not exist and the gateway stays unstarted
(fail closed) until `docs/manual-steps.md` step 9.

### Manual runtime test

1. Complete `docs/manual-steps.md` step 9 (install ESO, resume
   `platform-observability-secrets`) and step 10 (digest pinning).
2. Emit the fixture as a data-app log line, e.g. from the Immich server pod:
   `kubectl -n immich exec deploy/immich-server -- printenv` is not enough; use the
   app's own logger or `kubectl -n immich logs` on a pod that printed it. The
   fixture text is `tests/observability/fixtures/immich-planted.json`.
3. Query the tenant through the gateway, using the same contract the broker has:

   ```sh
   TOKEN=$(kubectl -n observability get secret loki-gateway-token -o jsonpath='{.data.password}' | base64 -d)
   kubectl -n observability port-forward svc/loki-gateway 3100:3100 &
   curl -sS -H "Authorization: Bearer $TOKEN" \
     --get http://127.0.0.1:3100/loki/api/v1/query_range \
     --data-urlencode 'query={tier="data"}' --data-urlencode 'limit=100'
   ```

4. Expected: the entry for the planted line contains only the six fields; grep
   the response for `Vacation`, `IMG_1234` and the album path and expect no
   match. A second query with `X-Scope-OrgID: raw` against Loki directly must
   **not** return the line: it lives in `data`.

### Runtime result

Not executed: needs the live cluster (no kubelet, Loki or ESO here). Static half
passes: `tests/observability/check.sh` → `observability: 29 passed, 0 failed`,
Vector 0.61.0, Python 3.13.

## A data app cannot mount another app's share, and dumps land before restic

SDLC #580. Property under test: a pod in one data namespace cannot mount a
PVC from another data namespace (so Immich cannot read the music or books
library even after a compromise), a data PVC cannot be dynamically provisioned
or named outside its namespace, and the nightly database dumps are complete on
the share before the host's restic walk starts.

### What runs automatically (CI job `policy checks`)

Static, in `tests/policies/check.sh` (no cluster, no credentials):

- `data-pvc-scope` is bound to every data namespace and its CEL mentions the
  name prefix it enforces; `data-pvc-storage` likewise for the StorageClass.
- Fixture `data-pod-foreign-pvc.yaml` (an `immich` pod mounting
  `navidrome-data`) expects `deny data-pvc-scope`; `data-pod-own-pvc.yaml`
  (same pod, `immich-data`) expects allow; `data-pvc-bad-storage.yaml`
  (`storageClassName: standard`) expects `deny data-pvc-storage`.
- Fixture `immich-dump-pod.yaml` — the pod an `immich-dump` job creates — is
  admitted, so a policy tightening that broke the dump path fails CI.
- Every PV path is one of the guest's four virtiofs paths, every PV carries
  `reclaimPolicy: Retain`, `storageClassName: data-local`, node affinity
  `kubernetes.io/hostname: cluster`, and a `claimRef` naming exactly one app
  claim; each data namespace has exactly one PVC named `<namespace>-data`.
- No manifest under `clusters/homelab/data/` or `clusters/homelab/ingress/`
  contains `hostPath`.
- Both dump CronJobs run at `45 2 * * *` in `Europe/Berlin`, with
  `concurrencyPolicy: Forbid` and `activeDeadlineSeconds: 1800` (complete by
  03:15, before restic's 03:30 + 15 minute delay), mount the app's own claim,
  and write `/data/db-dumps/<app>.{dump,rdb}`.
- Every data workload runs as `data-workload`, carries
  `homelabs.dev/managed-by=platform`, runs as uid 1000 (the share's host owner),
  and sets limits and a probe; the dump jobs are exempt from the probe rule.
- The four `Ingress` hosts resolve to the expected Service and port, so the
  routing contract T7 mirrors cannot drift.

Runtime, `tests/policies/check.sh --runtime` (throwaway kube-apiserver): the new
fixtures are admitted/denied by the real API server, including the two new deny
cases. Recorded run 2026-11-11: `static+runtime: 474 passed, 0 failed`.

### What cannot run here

No CNI, so a NetworkPolicy is stored and shape-checked, not enforced; no
kubelet, so `local` PVs never bind and the pods' init containers never create
their subdirectories; no ESO, so `immich-db` and `neuralwatt-api-key` do not
exist and the pods stay unstarted; no scheduler, so the CronJobs are only
inspected.

### Manual runtime test (live cluster)

```sh
# 1. the isolation policies, against the real API server
cluster-kubectl -n immich apply --dry-run=server -f tests/policies/fixtures/data-pod-foreign-pvc.yaml
#    expected: Error from server (Forbidden): ... data-pvc-scope ...
cluster-kubectl -n immich apply --dry-run=server -f tests/policies/fixtures/data-pod-own-pvc.yaml
#    expected: pod/immich-server created (server dry run)

# 2. the storage contract
cluster-kubectl get pv                     # four PVs, each Bound to its own claim
cluster-kubectl -n immich get pvc          # immich-data Bound

# 3. the dump path, end to end
cluster-kubectl -n immich create job --from=cronjob/immich-dump dump-now
cluster-kubectl -n immich logs job/dump-now     # "immich dump written to /data/db-dumps/immich.dump"
sudo ls -l /var/lib/data/immich/db-dumps/immich.dump    # the same file, through the share

# 4. restic timing: the latest local snapshot holds that night's dump
sudo restic -r /var/backup/restic ls latest /var/lib/data/immich/db-dumps
```

### Runtime result

Static and API-server halves recorded above (2026-11-11). The node-level half —
PV binding, NetworkPolicy enforcement, the CronJob actually writing through the
share, and restic snapshotting that file — needs the live `cluster` guest and is
recorded in `docs/data-migration.md` "Verification on the live cluster".

## The digest cannot carry a prompt, and nothing can write to it

SDLC #595. Property under test: the digest document contains node/workload
health and six allowlisted activity fields per event; a planted prompt, message
body or tool payload from the `data` tenant is absent; the document is
deterministic and bounded at 64 KiB with at most 400 events; the job can reach
only the exporter and the gateway; and there is no Service or ingress rule that
would let Kanbei (or any tenant) send anything back to it.

### What runs automatically (CI job `cluster digest`)

`tests/digest/check.sh` runs on every push and pull request with no cluster and
no credentials. 63 assertions (recorded run, 2026-11-11):

| Family | Assertions |
| --- | --- |
| Builder unit tests: parsing, node/pod/workload health, restarts by pod prefix, versions, redaction, determinism, bounds, freshness, publication (30 cases) | 1 |
| The shipped ConfigMap embeds the tested `digest_builder.py` byte for byte | 1 |
| Namespace, tier label and the token-free ServiceAccount | 4 |
| Schedule (`*/15`, UTC), `Forbid`, deadlines, retry bound | 6 |
| Pod/container hardening (uid 1000, no token, read-only rootfs, seccomp) and the pinned image | 5 |
| Environment contract (share path, 64 KiB, 400 events, 96 files, 15m window, both source URLs) | 7 |
| Secret scope: exactly `loki-gateway-token`, no env secret reference, `password` projected as `token`, token file, claim mount | 5 |
| Storage: claim on `data-local`, PV path/claimRef/affinity/retention | 4 |
| Egress: exactly the two sources plus DNS, no rule without `to`, no `ipBlock`, ports 53/3100/9101, deny list from the fixture (`tenant-apps`, `broker`, `ingress`, `platform`, `0.0.0.0/0`, `::/0`, 443, 80) | 16 |
| No Service in the build and no Service selects the digest pod | 2 |
| No reverse path: nine namespaces have no digest-sourced ingress, the digest is a source only for the exporter/gateway policies, no RBAC names it | 11 |
| Fixture run: deterministic bytes, planted strings absent, under 64 KiB, three events | 1 |

The deny-by-construction part is in `digest_builder.activity_from_loki`: the
event object is built from `ACTIVITY_ALLOWLIST`, never filtered from the parsed
line. The negative assertions on the planted fixture are that the strings
`PROMPT`, `Vacation`, `IMG_1234`, `read_file`, `shadow` and `diary` do not occur
anywhere in the encoded document.

The static fixtures are `tests/digest/fixtures/loki-planted.json` (the planted
streams), `status-metrics.txt` (an exporter scrape), `egress.tsv` (the only
allowed destinations and the deny list) and `reverse-path.txt` (namespaces that
must never receive digest-sourced ingress).

### What cannot run here

No Loki, so the query is a fixture rather than a live `query_range`; no
status exporter, so node/workload health is a captured scrape; no CNI, so the
egress and ingress policies are shape-checked rather than enforced; no PV
provisioner, so the claim stays Pending until the static PV and the share
exist; and no host writer, so the age-encrypted batch and the inbox arrival are
operator steps.

### Manual runtime test

1. Complete `docs/manual-steps.md` step 23 (share, PV bound, first run). It
   includes the nix-config `dataShares` entry this stage depends on.
2. Run the job once and inspect the document. `jq` on the job's host-side file
   is enough; the file holds classification only.

   ```sh
   kubectl -n observability create job --from=cronjob/cluster-digest cluster-digest-manual
   kubectl -n observability logs job/cluster-digest-manual -f
   ssh smortress 'jq -r ".schema_version, .generated_at, .source_freshness, (.nanobot_activity.events|length)" \
     /var/lib/data/cluster-digest/latest.json'
   ```

3. Expected: `"1"`, a UTC timestamp no older than 15 minutes, both sources
   `ok: true`, and an event count at or below 400. Grep the host-side file for a
   planted prompt and expect no match.
4. Reverse-path test: `kubectl -n observability get svc | grep cluster-digest`
   must print nothing, and a tenant pod dialling the digest by any name must
   fail because no Service or ingress rule exists.

### Runtime result

Not executed: needs the live cluster (no Loki, CNI, PV provisioner or host
writer here). Static half passes: `tests/digest/check.sh` → `digest: 63 passed,
0 failed`.

## nanobot has no credentials and no internal reach

SDLC #582. Property under test: the agent can call only its enrolled logical
hosts through the broker, and only with the credential the broker injects. It
cannot reach a private, loopback, link-local, CGNAT, pod-CIDR, service-CIDR or
node address even by naming one; cannot follow a redirect to another host;
cannot create a ServiceAccount or Secret, read `pods/log`, exec into a data pod,
or patch a deployment; and cannot present its own credential to an upstream.
The broker logs logical host, method, path, status and bytes and never logs a
header, a body, a query string or a secret.

### What runs automatically (CI job `broker unit tests`)

`python3 -m unittest discover -s apps/broker/tests -t apps/broker` — 52 tests,
no network and no cluster. The resolver and connector are injected, so the real
policy code runs against scripted DNS answers and a loopback upstream.

| Family | Tests |
| --- | --- |
| Address policy: pod CIDR, loopback, link-local/metadata, CGNAT, multicast, unspecified, ULA, IPv4-mapped forms, non-global ranges, private-only enrollment, node address | 7 |
| DNS: pin first allowed address, one refused address denies the answer, private answer needs a private enrollment, failure/empty are 502, loopback names never resolve | 5 |
| Redirects: relative and same-host pass; other host, parent/child host, other port, scheme change and unparsable port deny | 2 |
| Request shape: absolute-form and `//` refused, IP-literal `Host` refused, encoded NUL/backslash refused, dot segments canonicalized, query preserved | 6 |
| Enrollment/secret loading: shipped config is the logical-host contract, no credential value in it, invalid host/port/header/kind/enrollment rejected, env deny-list, missing/empty secret is fatal | 15 |
| Redaction: raw/URL/JSON/slash-escaped/base64 forms replaced, longest variant wins, URL-bearing headers only | 5 |
| Broker end-to-end: one injected credential, agent headers stripped, Loki tenant header forced, uncredentialed host forwarded, log shape and no secrets/query, cross-host redirect denied, same-host redirect returned, body redaction, CONNECT 405, IP Host 400, unknown host 404, POST body forwarded, pod address refused before connect | 12 |

### What runs automatically (CI job `policy checks`, static)

`tests/policies/check.sh` — 329 assertions (recorded run, 2026-11-11):
the previous table in this document describes the #578 families; #582 adds:

- `agent-identity` and `broker-runtime` are paired with bindings and select
  `tenant-apps` and `broker` respectively; their CEL mentions the fields the
  fixtures violate (service account, token automount, gvisor, read-only rootfs);
- `agent-debug` exists in all four data namespaces, grants exactly events
  `get`/`list` and pods `delete`, and binds only `tenant-apps/nanobot`;
- the `nanobot` ServiceAccount exists with `automountServiceAccountToken: false`;
- the broker Deployment is on gVisor, non-root, read-only, drops `ALL`, has no
  Secret-backed env, mounts `broker-credentials` fail-closed, is digest-pinned,
  and its Service listens on 8080;
- the broker build holds no Secret; the enrollment file names exactly the seven
  logical hosts, only `loki`/`status` allow private addresses, `loki` forces
  `X-Scope-OrgID: data`, and the SOPS contract keys equal the enrolled secret
  names; no token-shaped value is in the enrollment;
- broker egress is TCP 443 to global unicast with every non-global range in
  `except`; the tunnel reaches `tenant-apps/nanobot:8080` and nothing else;
- the stage DAG includes `platform-broker` and the apply list names it.

### What runs on demand (runtime, real API server)

`tests/policies/check.sh --runtime` — `static+runtime: 474 passed, 0 failed`
(recorded run, 2026-11-11, kube-apiserver v1.36.2, etcd 3.6.13). The new
fixtures are admitted/denied by the real API server:

- `agent-pod-wrong-sa.yaml` and `agent-pod-automount.yaml` → denied by
  `agent-identity`;
- `broker-pod-no-gvisor.yaml` and `broker-pod-writable-root.yaml` → denied by
  `broker-runtime`; `broker-pod-good.yaml` → admitted;
- `broker-pod-hostnetwork.yaml` now also satisfies `broker-runtime`, so its
  denial is attributable to `no-host-access`.

The `can-i` table (`tests/policies/rbac.tsv`, subject column) evaluates
`system:serviceaccount:tenant-apps:nanobot`: `delete pods` and `get`/`list
events` are `yes` in each data namespace; `get pods`, `pods/log`, `pods/exec`,
`pods/attach`, `pods/portforward`, `pods/proxy`, `get secrets`, `get nodes`,
`patch deployments`, and creating ServiceAccounts, Secrets or Roles in
`tenant-apps` are all `no`.

### What cannot run here

No CNI, so the NetworkPolicy pairs are stored and shape-checked, not enforced.
No kubelet or gVisor handler, so the runtime class is admitted but never
starts. No real DNS, TLS or provider tokens, so the unit tests inject the
resolver and connector and the live calls are manual. No cluster tunnel, so the
`agent.sammohr.dev` route and Access policies are only diffed and documented
(`docs/broker.md`). The broker image is a marked digest placeholder until
`docs/manual-steps.md` publishes it.

### Manual runtime test (live cluster)

```sh
# 1. the broker is up and the enrollment is the shipped one
cluster-kubectl -n broker get deploy broker
cluster-kubectl -n broker exec deploy/broker -- \
  python -c 'import json,sys; print(sorted(json.load(open("/etc/broker/broker.json"))["logical_hosts"]))'

# 2. from the agent pod: enrolled call works, direct/private call does not
cluster-kubectl -n tenant-apps exec deploy/nanobot -- \
  curl -sS -o /dev/null -w '%{http_code}\n' "$BROKER_URL/neuralwatt/v1/models"
cluster-kubectl -n tenant-apps exec deploy/nanobot -- \
  curl -sS -o /dev/null -w '%{http_code}\n' \
  "http://broker.broker.svc.cluster.local:8080/neuralwatt/../status/metrics"   # 400/403
cluster-kubectl -n tenant-apps exec deploy/nanobot -- \
  curl -sS --max-time 5 http://10.43.0.1:443/                              # times out (NetworkPolicy)

# 3. the agent's token is absent and its RBAC is the ceiling
cluster-kubectl -n tenant-apps get sa nanobot -o jsonpath='{.automountServiceAccountToken}\n'
cluster-kubectl auth can-i delete pods -n immich --as system:serviceaccount:tenant-apps:nanobot   # yes
cluster-kubectl auth can-i get pods/log -n immich --as system:serviceaccount:tenant-apps:nanobot   # no
cluster-kubectl auth can-i patch deployments.apps -n immich --as system:serviceaccount:tenant-apps:nanobot  # no

# 4. every request was logged with metadata only
cluster-kubectl -n broker logs deploy/broker | tail -5
```

### Runtime result

Static and API-server halves executed on 2026-11-11 (`361` and `474` passing);
the unit suite is green in CI. The live half needs the cluster, the broker
image and the SOPS credentials, and is recorded here when the coordinator runs
it.
