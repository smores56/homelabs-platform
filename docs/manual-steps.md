# Manual steps

Everything here needs a human: account creation, 2FA, token issuance, and the
one-time GitHub settings that `gh api` is allowed to change but an agent must
not. Record the date and who ran it when it is done.

## 1. Create the nanobot machine user

nanobot forked from `homelabs-platform` cannot be the owner's own account
(GitHub refuses a fork of your own repository), so it needs a second identity.

1. Create a dedicated GitHub account, e.g. `smores56-nanobot`, with an email
   address the owner controls. Enable 2FA with a separate TOTP secret stored in
   the owner's password manager; do not reuse the owner's credentials.
2. Add the account as an outside collaborator:
   - `smores56/homelabs-platform`: **read** (Metadata read + Contents read).
   - `smores56/homelabs-apps`: **write** (contents read/write, pull requests
     read/write).
3. Fork and PR contract on the platform repo:
   - If GitHub accepts `POST /repos/smores56/homelabs-platform/forks` for a
     read-only collaborator, keep read access: the fork is the only place the
     account can push, and `docs/negative-tests.md` tests that boundary.
   - If GitHub rejects the fork for read-only access, grant **write** instead.
     Branch protection still keeps the account off `main` (PR required, no
     force-push, required checks), but the fork no longer isolates the account,
     so the `fork-pr-secret-boundary` job stays the load-bearing control and the
     environment restriction below is what protects the token.
4. Credentials for the broker (logical host contracts, not GitHub logins):
   - `github-push-apps`: fine-grained PAT, resource owner `smores56`, repository
     access `homelabs-apps` only, permission **Contents: read and write**,
     **Pull requests: read and write** (for branch pushes and no direct main
     access), expiry 90 days.
   - `github-fork-platform`: fine-grained PAT, repository access
     `homelabs-platform`, permission **Contents: read**, **Pull requests: read
     and write**, expiry 90 days. Used only for fork sync and PR creation
     against the platform repo; the broker strips it on every other host.
   Record both PATs in the broker's secret store, never in this repository.

## 2. Create the `cloudflare` environment secret

The environment exists (created by `gh api`, see `docs/runbook.md`); the secret
value can only be set by a human.

```sh
read -rsp 'Cloudflare API token: ' CLOUDFLARE_API_TOKEN
gh secret set CLOUDFLARE_API_TOKEN --env cloudflare --repo smores56/homelabs-platform
unset CLOUDFLARE_API_TOKEN
```

Verify without printing the value:

```sh
gh api repos/smores56/homelabs-platform/environments/cloudflare/secrets \
  --jq '.secrets[].name'
```

Expect `CLOUDFLARE_API_TOKEN`. An environment with no secret is why
`tofu-plan.yml` logs `CLOUDFLARE_API_TOKEN absent; skipping plan` instead of
failing; `tofu-apply.yml` fails loudly in that state.

## 3. Scope the Cloudflare API token

Create the token in the Cloudflare dashboard under My Profile → API Tokens →
Create Custom Token, then store it in the password manager and in the
environment secret from step 2.

- Permissions (least privilege for `tofu/`):
  - Account → **Access: Apps and Policies: Edit**
  - Account → **Access: Organizations, Identity Providers, and Groups: Read**
  - Account → **Zero Trust: Edit** (network hostname routes)
  - Zone → **DNS: Edit** (CAA record)
  - Zone → **Zone: Read** (zone lookup)
- Account resources: Include → the owner's account only.
- Zone resources: Include → Specific zone → `sammohr.dev`.
- Client IP filtering: leave unset. GitHub-hosted runners have no stable
  egress address, so the token is protected by the `cloudflare` environment
  being restricted to `main` with no bypass, not by an IP allowlist.
- TTL: 90 days. Rotate by creating the replacement, updating the environment
  secret, and deleting the old token after the next successful `tofu-apply`.

A token that can repoint `kanbei.sammohr.dev` is why diffing `*.tf` and
`.github/` is Kanbei-critical (`docs/kanbei-critical.md`).

## 4. Apply branch protection

Run the commands from `docs/runbook.md` section "Branch protection on main".
They require an admin token; `enforce_admins` is false so the owner can still
push a bootstrap commit, while the machine user and every other account are
bound by PR review and the required checks.

After applying, confirm the machine user cannot push to `main` (expected:
`protected branch hook declined`) and cannot force-push (expected: `Cannot
force-push to a protected branch`). Record both outputs beside the runbook.
