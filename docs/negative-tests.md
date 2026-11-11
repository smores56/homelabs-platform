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

Result of the static half: **PASS** locally on the bootstrap commit
(`scripts/check-pr-secret-boundary.sh` → `no pull_request workflow references
secrets beyond GITHUB_TOKEN`). It also passes inside job
`fork-pr-secret-boundary` on the first PR once branch protection is active.

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
