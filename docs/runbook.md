# Runbook

Repository `smores56/homelabs-platform`. Updated 2026-11-11 at bootstrap commit
`4d1229f`, `gh` 2.96.0, owner `smores56`.

The token available to the bootstrap agent is a fine-grained PAT scoped to
repository contents, issues and pull requests. Every endpoint below needs
**Administration: write** on the repository (classic `repo` scope also works),
so environment, branch-protection and Actions-setting calls returned HTTP 403
and remain owner actions. Exact responses are recorded under each step.

## Bootstrap push (done)

```sh
git add -A && git commit -m "feat(platform): bootstrap repo, tenancy namespaces, and CI guards"
git push -u origin main
# To https://github.com/smores56/homelabs-platform.git
#  * [new branch]      main -> main
```

## Environment `cloudflare` (blocked: 403)

Custom branch policy, so only `main` may deploy; no required reviewers, because
the owner is the only approver and a self-review gate only adds a click.

```sh
gh api -X PUT repos/smores56/homelabs-platform/environments/cloudflare --input - <<'JSON'
{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON

gh api -X POST repos/smores56/homelabs-platform/environments/cloudflare/deployment-branch-policies \
  -f name=main -f type=branch
```

Recorded responses:

```json
{"message":"Resource not accessible by personal access token","documentation_url":"https://docs.github.com/rest/deployments/environments#create-or-update-an-environment","status":403}
{"message":"Resource not accessible by personal access token","documentation_url":"https://docs.github.com/rest/deployments/branch-policies#create-a-deployment-branch-policy","status":403}
```

Actual state after the attempts:

```sh
gh api repos/smores56/homelabs-platform/environments --jq '.environments[] | {name, created_at}'
# {"name":"cloudflare","created_at":"2026-11-11T12:51:13Z"}

gh api repos/smores56/homelabs-platform/environments/cloudflare --jq \
  '{name, protection_rules, deployment_branch_policy}'
# {"name":"cloudflare","protection_rules":[],"deployment_branch_policy":null}

gh api repos/smores56/homelabs-platform/environments/cloudflare/deployment-branch-policies
# 404 Not Found (no policies exist)
```

So the environment object exists but **no branch restriction is in force**:
the token could not set `deployment_branch_policy`, and the PUT ended in 403
without applying it. Until the owner sets the `main` policy, any branch could
consume the environment. No environment secret exists yet
(`GET .../environments/cloudflare/secrets` → 403; `CLOUDFLARE_API_TOKEN` is
unset, `docs/manual-steps.md` step 2), so there is nothing to consume today.
Order matters: set the branch policy, then the secret.

Verify once an admin token is used:

```sh
gh api repos/smores56/homelabs-platform/environments/cloudflare --jq \
  '{name, protection_rules, deployment_branch_policy}'
gh api repos/smores56/homelabs-platform/environments/cloudflare/deployment-branch-policies --jq \
  '.branch_policies[] | {name, type}'
```

Expected: `deployment_branch_policy.custom_branch_policies == true`, branch
policy list `main`/`branch`, and `protection_rules == []` (no required
reviewers, no wait timer).

## Branch protection on `main` (blocked: 403)

PR required with CODEOWNERS review, required CI checks, linear history, no
force-push, no deletion, conversation resolution. `enforce_admins` stays false so
the owner can bootstrap; the machine user and every other account are bound.

```sh
gh api -X PUT repos/smores56/homelabs-platform/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "kustomize build",
      "kubeconform",
      "yamllint",
      "tofu fmt and validate",
      "shellcheck",
      "action SHA pins",
      "CODEOWNERS coverage",
      "policy checks",
      "fork-pr-secret-boundary"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
```

Recorded response:

```json
{"message":"Resource not accessible by personal access token","documentation_url":"https://docs.github.com/rest/branches/branch-protection#get-branch-protection","status":403}
```

(The GET and PUT endpoints returned the same 403; the PUT was not attempted
after the GET proved the token lacks the scope.)

Verify once an admin token is used:

```sh
gh api repos/smores56/homelabs-platform/branches/main/protection --jq \
  '{required_status_checks: .required_status_checks.contexts,
    reviews: .required_pull_request_reviews.require_code_owner_reviews,
    linear: .required_linear_history.enabled,
    force_push: .allow_force_pushes.enabled,
    deletions: .allow_deletions.enabled}'
```

Expected: `require_code_owner_reviews == true`, `linear == true`,
`force_push == false`, `deletions == false`, and the nine contexts above.

## Repo settings (blocked: 403)

Read-only default token for every workflow, forks get no write token, and
private vulnerability reporting on.

```sh
# Actions enabled, only GitHub-owned + verified Marketplace actions allowed.
gh api -X PUT repos/smores56/homelabs-platform/actions/permissions --input - <<'JSON'
{"enabled":true,"allowed_actions":"selected","sha_pinning_required":true,
 "selected_actions_url":"https://api.github.com/repos/smores56/homelabs-platform/actions/permissions/selected-actions"}
JSON
gh api -X PUT repos/smores56/homelabs-platform/actions/permissions/selected-actions --input - <<'JSON'
{"github_owned_allowed":true,"verified_allowed":true}
JSON

# Default GITHUB_TOKEN is read-only and cannot approve PRs.
gh api -X PUT repos/smores56/homelabs-platform/actions/permissions/workflow \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false

# Every fork PR run waits for an owner approval; fork runs get a read-only token.
gh api -X PUT repos/smores56/homelabs-platform/actions/permissions/fork-pr-contributor-approval \
  -f approval_policy=all_external_contributors

# Private vulnerability reporting.
gh api -X PUT repos/smores56/homelabs-platform/private-vulnerability-reporting --input - <<<'{}'
```

Recorded responses:

- `GET /actions/permissions`, `/actions/permissions/workflow`,
  `/actions/permissions/fork-pr-contributor-approval`:
  `{"message":"Resource not accessible by personal access token","documentation_url":"https://docs.github.com/rest/actions/permissions#get-github-actions-permissions-for-a-repository","status":403}`
- `PATCH /repos/smores56/homelabs-platform`:
  `{"message":"Resource not accessible by personal access token","documentation_url":"https://docs.github.com/rest/repos/repos#update-a-repository","status":403}`

Current state from the same token: repo is private,
`default_branch == "main"`, `allow_forking == true`, and the owner has `admin`,
so the endpoints above are scope failures, not plan failures.

Verify once an admin token is used:

```sh
gh api repos/smores56/homelabs-platform/actions/permissions/workflow
gh api repos/smores56/homelabs-platform/actions/permissions/fork-pr-contributor-approval
gh api repos/smores56/homelabs-platform/private-vulnerability-reporting
```

Expected: `{"default_workflow_permissions":"read","can_approve_pull_request_reviews":false}`,
`{"approval_policy":"all_external_contributors"}`, and `{"enabled":true}`.

## Scheduled sanity checks

```sh
scripts/check-action-pins.sh
scripts/check-pr-secret-boundary.sh
scripts/check-codeowners.sh
scripts/kustomize-build-all.sh
tests/policies/check.sh
nix shell nixpkgs#yamllint nixpkgs#shellcheck nixpkgs#opentofu -c bash -c \
  'yamllint -c .yamllint.yaml $(git ls-files "*.yaml" "*.yml") && shellcheck scripts/*.sh tests/policies/*.sh clusters/homelab/flux-system/bootstrap.sh && tofu fmt -check -recursive tofu'
```

All of the above pass on the bootstrap commit; see `docs/negative-tests.md` for
the fork-PR boundary and `docs/manual-steps.md` for the owner-only steps.

## CI evidence on `main` (2026-11-11)

Runs triggered by the bootstrap pushes, state read from the Actions API:

| Workflow | Run | Result |
| --- | --- | --- |
| `ci` | [43024148131](https://github.com/smores56/homelabs-platform/actions/runs/43024148131) | success (`kustomize build`, `kubeconform`, `yamllint`, `tofu fmt and validate`, `shellcheck`, `action SHA pins`, `CODEOWNERS coverage` all success; `fork-pr-secret-boundary` skipped on push) |
| `tofu-plan` | [43024148058](https://github.com/smores56/homelabs-platform/actions/runs/43024148058) | success; logs `CLOUDFLARE_API_TOKEN absent; skipping plan` |
| `tofu-apply` | [43024148159](https://github.com/smores56/homelabs-platform/actions/runs/43024148159) | failure; step `apply` errors with `CLOUDFLARE_API_TOKEN absent; cannot apply` |

The `tofu-apply` failure is the intended loud signal until
`docs/manual-steps.md` step 2 stores the token: with the environment secret
absent, the workflow cannot apply and must not pretend it did. It also ran
without an approval prompt because the environment has no protection rules,
which is why the missing `main` branch policy in the section above matters.

`ci` on the first push (`4d1229f`) has no run entry; the workflow files landed
in that same commit, so the first observable run is on `c5a6dca`.

## Cluster bring-up: bootstrap and the three operator steps

The platform stack is bootstrapped as code (`docs/flux.md`); three things are
human-held and must be created before the stack converges.

### 1. Bootstrap Flux

```sh
GITHUB_TOKEN=$(gh auth token) clusters/homelab/flux-system/bootstrap.sh
flux -n flux-system get kustomizations
```

Expected: `flux-system` Kustomization Ready on the pinned
`serviceAccountName: kustomize-controller` (see `docs/flux.md` for why bootstrap
re-renders `gotk-sync.yaml`), then the six `platform-*` stages appear in order.

### 2. Age key for SOPS

One key per namespace that holds ciphertext. Generate on the workstation, never
in the cluster, never in git:

```sh
age-keygen -o sops-age.agekey
export AGE_RECIPIENT=$(age-keygen -y sops-age.agekey)
kubectl -n flux-system create secret generic sops-age \
  --from-file=age.agekey=sops-age.agekey
kubectl -n tenant-apps create secret generic sops-age \
  --from-file=age.agekey=sops-age.agekey
rm sops-age.agekey   # the only copy lives in the password manager
```

Store `sops-age.agekey` in the password manager before deleting it. Then:

```sh
kubectl -n flux-system get secret sops-age -o jsonpath='{.data.age\.agekey}' | base64 -d | head -1
```

Expected: the `AGE-SECRET-KEY-1...` line. The `sops-contract` ConfigMap in
`flux-system` records the same names; the controller flag `--sops-age-secret` is
never set (`docs/flux.md`).

### 3. Tenant repository credential

`homelabs-apps` is private, so the GitRepository in `tenant-apps` needs its own
read-only credential in the same namespace:

```sh
flux create secret git homelabs-apps-auth \
  --namespace=tenant-apps \
  --url=ssh://git@github.com/smores56/homelabs-apps
```

Add the printed public key to `smores56/homelabs-apps` under Settings → Deploy
keys with **Allow write access clear**. Verify:

```sh
flux -n tenant-apps get gitrepository        # READY True
flux -n tenant-apps get kustomization        # homelabs-apps reconciled as tenant-apps-reconciler
```

### 4. External Secrets, then resume its stage

The ESO objects ship suspended because the operator's CRDs are not installed by
this repository:

```sh
helm repo add external-secrets https://charts.external-secrets.io
helm repo update
helm install external-secrets external-secrets/external-secrets \
  --namespace platform --create-namespace
flux -n flux-system resume kustomization platform-secrets-eso
kubectl -n tenant-apps get externalsecret
```

Expected: `nanobot-broker-token` Secret materialises from the `Password`
generator (`refreshInterval: "0"`, so it mints once and stays stable until the
target Secret is deleted).

### 5. Confirm the policy stack on the live cluster

```sh
tests/policies/check.sh            # 184 static assertions, no cluster
nix shell nixpkgs#etcd nixpkgs#kubernetes nixpkgs#openssl nixpkgs#kustomize \
  nixpkgs#yq-go nixpkgs#jq -c tests/policies/check.sh --runtime
kubectl get validatingadmissionpolicies,validatingadmissionpolicybindings
kubectl auth can-i create secrets -n tenant-apps \
  --as=system:serviceaccount:tenant-apps:tenant-apps-reconciler   # no
```

The one check the ephemeral bench cannot make is PSA at `restricted`:

```sh
kubectl -n tenant-apps run probe --image=busybox \
  --overrides='{"spec":{"runtimeClassName":"gvisor","volumes":[{"name":"h","hostPath":{"path":"/"}}]}}'
```

Expected: rejected with a `violates PodSecurity "restricted"` violation
(`docs/negative-tests.md`).

### Required CI contexts

`.github/workflows/ci.yml` now includes the `policy checks` job. The
branch-protection payload in "Branch protection on `main`" must list nine
contexts once the owner applies it: `kustomize build`, `kubeconform`,
`yamllint`, `tofu fmt and validate`, `shellcheck`, `action SHA pins`,
`CODEOWNERS coverage`, `policy checks`, `fork-pr-secret-boundary`.
