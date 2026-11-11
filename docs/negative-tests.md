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
and pull request. 184 assertions (recorded run, 2026-11-11):

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
| Stage DAG (`dependsOn`), stage service accounts, apply order | 26 |
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
- 30 fixture admissions: 10 allowed, 20 denied, each denial matched to the policy
  named in `fixtures/expectations.tsv` by its denier string;
- 27 `can-i` probes from `tests/policies/rbac.tsv`, run as
  `system:serviceaccount:tenant-apps:tenant-apps-reconciler`;
- 2 live attempts over the API (create a Secret, create a Kustomization), both
  `forbidden`.

Result: `static+runtime: 250 passed, 0 failed`.

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
`tests/policies/check.sh --runtime` → `static+runtime: 250 passed, 0 failed`.
The same checker runs without `--runtime` in CI, so a regression in any static
assertion fails the pull request that introduces it.
