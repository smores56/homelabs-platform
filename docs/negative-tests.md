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
and pull request. 192 assertions (recorded run, 2026-11-11):

| Family | Assertions |
| --- | --- |
| `kustomize build clusters/homelab` renders, every `clusters/` and `tests/` YAML file parses (`yq`) | 2 |
| Policy/binding pairing, names, `failurePolicy: Fail` | 45 |
| Each binding selects exactly the namespaces in `tests/policies/bindings.tsv`, resolved against the real namespace labels | 9 |
| Each fixture's expected policy really contains the CEL field the fixture violates | 23 |
| RBAC deny list, wildcard-free rules, no cluster-scoped objects, only the tenant SA bound | 9 |
| Rendered controller `args` match the reviewed flag table (`docs/flux.md`) | 13 |
| No insecure kubeconfig flags anywhere in manifests or scripts | 1 |
| NetworkPolicy shape: default-deny both directions, DNS to `kube-system`, broker-only tenant egress, immich from `ingress` only, no extra data flows | 12 |
| Stage DAG (`dependsOn`), stage service accounts, apply order | 34 |
| `gotk-sync.yaml` equals `bootstrap.sh --print-sync`, bootstrap flags, tenant Kustomization pins | 13 |
| Fixture/expectation table completeness and `# expect:` headers | 31 |

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
`gotk-components.yaml` (for the Flux CRDs), then the six platform stages in order,
then drives every case with `kubectl --dry-run=server` and `kubectl auth can-i`.
66 assertions (recorded run, 2026-11-11, kube-apiserver v1.36.2, etcd 3.6.13):

- 6 stage applies plus the Flux component apply;
- 30 fixture admissions: 7 allowed, 23 denied, each denial matched to the policy
  named in `fixtures/expectations.tsv` by its denier string;
- 27 `can-i` probes from `tests/policies/rbac.tsv`, run as
  `system:serviceaccount:tenant-apps:tenant-apps-reconciler`;
- 2 live attempts over the API (create a Secret, create a Kustomization), both
  `forbidden`.

Result: `static+runtime: 258 passed, 0 failed`.

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
`tests/policies/check.sh --runtime` → `static+runtime: 258 passed, 0 failed`.
The same checker runs without `--runtime` in CI, so a regression in any static
assertion fails the pull request that introduces it.

## Data-tenant logs carry classification only

SDLC #579. Property under test: a planted Immich log line that names a file and
an album reaches Loki's `data` tenant as `level`, `component`, `error_code`,
`status`, `namespace` and `timestamp` and nothing else; a non-data namespace
ships raw; and the `data` tenant is unreachable without the gateway token.

### What runs automatically (CI job `observability pipeline`)

`tests/observability/check.sh` runs on every push and pull request with no
cluster and no credentials. 29 assertions (recorded run, 2026-11-11):

| Family | Assertions |
| --- | --- |
| `vector test` on `tests/vector/vector.yaml`: planted fixture loses body, text log keeps only a level token, JSON without classification keeps metadata only, namespace-label routing for data/tenant/unlabelled | 6 cases in 1 assertion |
| `vector validate --no-environment` on the shipped config, with the VRL staged where the container mounts it | 1 |
| Shipped-vs-test drift: `tenant_route` source, both filter conditions, both `file:` paths, the `vector-vrl` generator entry | 5 |
| The planted fixture really carries `file`, `albumName`, `originalPath`, and the unit-test input parses to the same JSON | 2 |
| Tenant separation: no `tenant_id` on the data sink, gateway endpoint, bearer auth, raw tenant direct, Loki `auth_enabled`, 168h retention, telemetry off | 7 |
| Token contract: gateway and Vector read `loki-gateway-token`, ExternalSecret targets it, generator kind is `Password` | 4 |
| Status exporter RBAC: resources and verbs exact, **no ClusterRoleBinding**, binding namespaced to `data`, subject in `observability` | 5 |
| Postgres fragment sets `terse` + `panic`; every observability image is version/digest pinned | 2 |
| Python unit tests for `exporter.py` and `gateway.py` | 29 tests in 1 assertion |

The deny-by-construction part is in the VRL itself: the event is rebuilt from an
allowlist, so a parser failure or an unknown log format cannot fall back to
shipping the line. The negative assertions on the planted fixture are
`!exists(.message)`, `!exists(.file)`, `!exists(.albumName)`,
`!exists(.originalPath)`, and `!contains(encode_json(.), "Vacation" | "IMG_1234" | "Alice")`.

Two properties are asserted statically rather than exercised: the routing
comparison (unit tests call the same `tenant_route` source the DaemonSet ships,
and the drift check fails if the two strings differ) and the gateway's tenant
header (unit tests prove the client's `X-Scope-OrgID` is replaced).

### What cannot run here

No kubelet, so `kubernetes_logs` never reads `/var/log/pods`; no Loki, so no
batch is actually accepted; no Immich, so the line is planted through a Vector
unit test rather than through a container log; no CNI, so the broker→gateway
NetworkPolicy is only shape-checked; no ESO, so `loki-gateway-token` does not
exist and both readers stay unstarted (fail closed) until `docs/manual-steps.md`
step 9.

### Manual runtime test

1. Complete `docs/manual-steps.md` step 9 (install ESO, resume
   `platform-observability-secrets`) and step 10 (digest pinning).
2. Emit the fixture as a data-app log line, e.g. from the Immich server pod:
   `kubectl -n data exec deploy/immich-server -- printenv` is not enough; use the
   app's own logger or `kubectl -n data logs` on a pod that printed it. The
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
