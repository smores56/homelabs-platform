# Manual steps

Everything here needs a human: account creation, 2FA, token issuance, and the
one-time GitHub settings that `gh api` is allowed to change but an agent must
not. Record the date and who ran it when it is done.

## 1. Create the `homelabs-agent` GitHub App

The agent's GitHub identity is an app, not a user: installation tokens are
short-lived, scoped per request by the broker, and revocable from the app's
settings page. There are no PATs for the agent anywhere.

A GitHub App cannot fork a repository, and GitHub refuses a fork of your own
private repository. The agent therefore pushes `agent/*` branches to
`homelabs-platform` and opens same-repo pull requests. Same-repo PRs run
`pull_request` workflows inside the base repository, so the fork boundary is
gone; the remaining controls are that no repository-level secrets exist (only
main-gated environment secrets), and the ruleset in item 3.

1. Create the app at https://github.com/settings/apps/new:
   - Name: `homelabs-agent` (its actor becomes
     `homelabs-agent[bot]`).
   - Homepage: `https://github.com/smores56/homelabs-platform`.
   - Webhook: **inactive** (the broker polls; there is no endpoint for GitHub
     to deliver to, and the tunnel admits nothing from GitHub).
   - Post installation: Setup URL blank, **Redirect on update** off.
   - Identifying and authorizing users: Callback URL blank;
     **Expire user authorization tokens** at its default (on) — it governs user
     access tokens only, which this app never issues; **Request user
     authorization (OAuth) during installation** off; **Enable Device Flow**
     off. Turning any of these on would create a second, human-impersonating
     token type the broker does not need and cannot audit.
   - Repository permissions: **Contents: read and write**, **Pull requests:
     read and write**, **Metadata: read** (mandatory); **Actions: read** for CI
     status and publish digests. Leave **Workflows** at **No access**: GitHub
     refuses a push touching `.github/workflows/**` with
     `refusing to allow a GitHub App to create or update workflow ... without
     workflows permission` (verified with the app token, 2026-11-12). A local
     probe must disable git's credential helper
     (`-c credential.helper= -c credential.https://github.com.helper=`), or it
     authenticates as the owner and the permission looks unenforced. The merge
     review is the backstop, not the gate.
   - Organization permissions, Account permissions, Enterprise permissions:
     **No access**, all of them. The app operates on two user-owned
     repositories through scoped installation tokens; anything broader is
     authority the broker would hold and never use.
   - Where can this app be installed: **Only on this account**.
2. Record the **App ID** from the app's settings page, generate a **private
   key** (downloads a `.pem`; keep it in the password manager), then install the
   app once on the `smores56` account with **all four** repositories selected:
   `homelabs-apps`, `homelabs-platform`, `nix-config` and `kanbei` (an
   installation is per account, not per repository: one ID covers all of them,
   and the broker narrows each minted token to the repositories named on the
   logical host it is serving). Record the **installation ID** from the install
   URL (`/installations/<id>`). The app id, key and installation id reach the
   broker only as files (step 10); nothing is committed. Every repository named
   on a logical host goes into one token request, so a repository that is in
   `broker.json` but not in the installation fails the whole host with a 422:
   add it to the installation first.
3. Contain the identity to `agent/**`: Settings -> Rules -> Rulesets -> New
   branch ruleset:
   - Target branches: **Include all branches**, exclude by pattern `agent/*`.
     Agent branches are exactly one segment (`agent/<slug>`): measured
     2026-11-12, a nested `agent/nested/probe` was refused even with the
     exclude written as `agent/**`, so do not assume `**` crosses `/` here.
     An **empty include** is inert: the ruleset is listed and
     `GET /rules/branches/...` reports its rules, but pushes are not blocked —
     verified live, so set the include explicitly.
   - Rules: Restrict creations, Restrict updates, Restrict deletions.
   - Bypass list: Repository admins (the human); leave the app out.
   Branch protection on `main` (step 5) requires a PR, passing checks, and a
   Code Owners review, so the app can neither merge nor force-push and a
   `.github/` or `*.tf` change needs a human review. Do not rely on the
   description: read the API's view back:
   `gh api repos/smores56/homelabs-platform/rulesets --jq '.[] | select(.target=="branch") | .id'`,
   then that id's `conditions.ref_name` (`include ["~ALL"]`,
   `exclude ["agent/**"]`), `rules[].type` (`creation`, `update`, `deletion`)
   and `bypass_actors`. The acceptance script also probes both refusals live
   (a push outside `agent/**` and a push touching `.github/workflows/`), so a
   silently removed control fails the run instead of waiting for an incident.
   The same two rulesets must exist on `nix-config` and `kanbei`, where the
   branch gate is named `agent-gating`: the broker's `github-agent-push` host
   can write `contents` on both, so the rulesets, not the token, are what keep
   the app off `main` and off every branch outside `agent/**`.
4. The mirror's `github-backup-read-pat` (step 22) is not the agent's identity;
   it stays an operator-owned fine-grained PAT.

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

The state backend's R2 pair is a second pair of environment secrets
(`R2_STATE_ACCESS_KEY_ID`, `R2_STATE_SECRET_ACCESS_KEY`); step 14 creates the
bucket and sets both, and `tofu-plan.yml` skips with a notice until they exist.

## 3. Scope the Cloudflare API token

Create the token in the Cloudflare dashboard under My Profile → API Tokens →
Create Custom Token, then store it in the password manager and in the
environment secret from step 2.

- Permissions (least privilege for `tofu/`):
  - Account → **Access: Apps and Policies: Edit** (applications, policies,
    bypass and service-auth decisions)
  - Account → **Access: Organizations, Identity Providers, and Groups: Read**
  - Account → **Cloudflare Tunnel: Edit** (the cluster tunnel's hostname routes)
  - Account → **Cloudflare One Networks: Edit** (same routes)
  - Account → **Cloudflare Pages: Edit** (the recovery project and its domain)
  - Account → **Workers R2 Storage: Edit** (the bucket CORS policy the recovery
    page needs)
  - Account → **Notifications: Edit** (tunnel health, service-token expiry)
  - Zone → **DNS: Edit** (records, wildcard, CAA)
  - Zone → **Zone: Read** (zone lookup)
  - Zone → **Zone Rulesets: Edit** (the public-app rate-limit ruleset)
- Account resources: Include → the owner's account only.
- Zone resources: Include → Specific zone → `sammohr.dev`.
- Client IP filtering: leave unset. GitHub-hosted runners have no stable
  egress address, so the token is protected by the `cloudflare` environment
  being restricted to `main` with no bypass, not by an IP allowlist.
- TTL: 90 days. Rotate by creating the replacement, updating the environment
  secret, and deleting the old token after the next successful `tofu-apply`.

A token that can repoint `kanbei.sammohr.dev` is why diffing `*.tf` and
`.github/` is Kanbei-critical (`docs/kanbei-critical.md`).

## 4. Create the `cloudflare` environment and apply repo settings

Both need **Administration: write** on the repository, which the bootstrap token
lacked (HTTP 403, recorded in `docs/runbook.md`). Run as the owner with a
classic `repo`-scoped PAT or a fine-grained PAT carrying Administration and
Actions write:

1. `docs/runbook.md` → "Environment `cloudflare`": the environment object
   already exists but has no deployment branch policy
   (`deployment_branch_policy: null`, branch-policy list 404). Set
   `custom_branch_policies: true` and add the `main` branch policy before any
   secret lands. No required reviewers.
2. `docs/runbook.md` → "Repo settings": read-only default `GITHUB_TOKEN`,
   SHA-pinning required, only GitHub-owned and verified actions, fork PRs need
   owner approval, private vulnerability reporting on.

Then confirm with the `gh api` verify commands in those sections. Do this before
setting the environment secret in step 2, because a secret can only be attached
to an environment that exists.

## 5. Apply branch protection

Run the commands from `docs/runbook.md` section "Branch protection on main".
They require the same admin token; `enforce_admins` is false so the owner can
still push a bootstrap commit, while `homelabs-agent[bot]` and every other
identity are bound by PR review, the required checks, and **Require review from
Code Owners** — the last one is what keeps `.github/` and `*.tf` (Kanbei-
critical) human-reviewed now that the app can technically push a workflow edit.

After applying, confirm the app cannot push to `main` (expected: `protected
branch hook declined`) and cannot force-push (expected: `Cannot force-push to a
protected branch`); the ruleset from step 1 covers every branch outside
`agent/*`. Record both outputs beside the runbook.

## 6. Create the SOPS age identity

The age private key is a credential and cannot be generated by an agent: nothing
here may hold it. One key per namespace that carries ciphertext (`flux-system`
for platform manifests, `tenant-apps` for `homelabs-apps`).

```sh
age-keygen -o sops-age.agekey
age-keygen -y sops-age.agekey            # record the age1... recipient
kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=sops-age.agekey
kubectl -n tenant-apps create secret generic sops-age --from-file=age.agekey=sops-age.agekey
# store the file in the password manager, then remove the local copy
rm sops-age.agekey
```

Verify (expect the `AGE-SECRET-KEY-1...` line, never paste its output into a
ticket):

```sh
kubectl -n flux-system get secret sops-age -o jsonpath='{.data.age\.agekey}' | base64 -d | head -1
kubectl -n tenant-apps get secret sops-age -o jsonpath='{.data.age\.agekey}' | base64 -d | head -1
```

The recipient goes into `.sops.yaml` when the first encrypted file lands; the
`--sops-age-secret` controller flag stays unset so each namespace decrypts only
with its own key (`docs/tenancy.md`).

## 7. Create the `homelabs-apps` deploy key

The tenant GitRepository reads a private repository from `tenant-apps`.

```sh
flux create secret git homelabs-apps-auth \
  --namespace=tenant-apps \
  --url=ssh://git@ssh.github.com:443/smores56/homelabs-apps
```

Take the printed public key, add it to `smores56/homelabs-apps` → Settings →
Deploy keys → **Allow write access: off**.

The URL is GitHub's SSH-over-443 endpoint because the cluster guest forwards only
80/443 out of the tap. An existing Secret created against `github.com` needs the
443 host key added (the identity is unchanged):

```sh
# Replace the host keys rather than appending: a file without a trailing
# newline glues the next line onto the last key and Flux then rejects it
# ("illegal base64 data").
ssh-keyscan -p 443 ssh.github.com 2>/dev/null > /tmp/kh
sudo cluster-kubectl -n tenant-apps patch secret homelabs-apps-auth --type merge \
  -p "{\"data\":{\"known_hosts\":\"$(base64 -w0 /tmp/kh)\"}}"
rm -f /tmp/kh
```

Then verify:

```sh
flux -n tenant-apps get gitrepository homelabs-apps -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
```

Expected: `True`.

## 8. Install External Secrets

The operator is not shipped by this repository, so installing it is a
precondition for the two ESO stages, not something they can wait for: both
`platform-secrets-eso` and `platform-observability-secrets` run unconditionally
and fail loudly on missing CRDs. Do not try to gate them with
`flux resume` — git owns those objects' spec, so a suspension set by hand
returns on the next `flux-system` reconcile.

```sh
helm repo add external-secrets https://charts.external-secrets.io
helm repo update
helm install external-secrets external-secrets/external-secrets \
  --namespace platform --create-namespace
kubectl -n platform rollout status deploy/external-secrets-webhook --timeout=120s
kubectl -n observability get externalsecret loki-gateway-token
kubectl -n broker get secret broker-credentials
```

Expected: `loki-gateway-token` is generated and owned by its ExternalSecret;
`broker-credentials` is the SOPS contract Secret from step 10. `loki-gateway-token`
is what unblocks the Loki gateway (its only reader besides the broker); Vector no
longer holds it, because both sinks write their tenant directly. A missing token
leaves the gateway unstarted (fail closed) and the broker's `loki` host dead. The agent holds no broker token, so there is no
`nanobot-broker-token` to generate. Rotate a generated token by deleting its
Secret and letting the `Password` generator mint a new one.

## 9. Digest-pin the observability images

The manifests pin version tags. Replace each tag with a digest once, from a
machine with registry access (the agent VM has none):

```sh
for ref in prom/prometheus:v3.16.0 grafana/loki:3.7.9 \
  timberio/vector:0.61.0-debian python:3.13.12-slim busybox:1.37.0 \
  ghcr.io/immich-app/immich-server:v3.0.3 \
  ghcr.io/immich-app/immich-machine-learning:v3.0.3 \
  ghcr.io/immich-app/postgres:17-vectorchord0.4.3-pgvector0.8.1 \
  deluan/navidrome:0.63.2 crocodilestick/calibre-web-automated:v4.0.8 \
  cloudflare/cloudflared:2026.10.2 alpine/git:2.54.0; do
  printf '%s -> %s\n' "$ref" "$(crane digest "$ref")"
done
```

Then rewrite those `image:` lines under
`clusters/homelab/observability/{prometheus,loki,vector,status-exporter,loki-gateway}`,
`clusters/homelab/data/{immich,navidrome,calibre,graphiti}`,
`clusters/homelab/ingress` and `clusters/homelab/platform/backup`
to `repo@sha256:<digest>` (keep the tag when the registry allows
`repo:tag@sha256:...`), run `scripts/kustomize-build-all.sh` and
`tests/observability/check.sh`, and commit. The image-pin assertion in
`tests/observability/check.sh` accepts either a version tag or a `sha256:` digest,
so a half-finished sweep still fails CI rather than shipping `latest`.

`alpine/git` is the one image already pinned, because the mirror job is the only
place a tag alone would ship: `docker.io/alpine/git:2.54.0` resolves to the
manifest list `sha256:6a1fdb962814ca11508ca8d1bab94375b5b7ac4e4a9a40981c29fc32c02f095d`
(recorded 2026-11-11; verify with `crane digest docker.io/alpine/git:2.54.0`
before trusting it). The nanobot image is pinned the same way in the
`homelabs-apps` repository; its placeholder digest is a deliberate CI failure
until that image is published and pinned (its `docs/manual-steps.md`, step 3).

## 10. Create the broker credential Secret

The broker is platform-owned (`clusters/homelab/broker/`); this step supplies
six files, none of which enters git. Static enrollments name three of them
(`keys` in `clusters/homelab/broker/sops-contract.yaml`); the GitHub App
enrollments add the app id, the private key and one installation id per
installation (`app_keys`). **Step 1 must be finished first.**

Run this on smortress as `smores`. `cluster-kubectl` reads a root-only
kubeconfig, hence `sudo`.

```sh
NW=$(cat /run/secrets/neuralwatt-api-key)
TF=$(cat /run/secrets/tinyfish-api-key)
LOKI=$(sudo cluster-kubectl -n observability get secret loki-gateway-token \
  -o jsonpath='{.data.password}' | base64 -d)
read -rp 'App ID (digits): ' APP_ID
read -rp 'installation ID: ' INSTALL
read -rsp 'private key path: ' KEY_PATH; echo

# No value reaches the command line or a transcript.
umask 077
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
printf '%s\n' "$NW" > "$tmp/neuralwatt-api-key"
printf '%s\n' "$TF" > "$tmp/tinyfish-api-key"
printf '%s\n' "$LOKI" > "$tmp/loki-gateway-token"
printf '%s\n' "$APP_ID" > "$tmp/github-app-id"
printf '%s\n' "$INSTALL" > "$tmp/github-installation-id"
install -m 0600 "$KEY_PATH" "$tmp/github-app-private-key"
unset NW TF LOKI APP_ID INSTALL

sudo cluster-kubectl -n broker create secret generic broker-credentials \
  --from-file="$tmp"
sudo cluster-kubectl -n broker rollout restart deployment/broker
```

The broker aborts at start if a static file is missing, and answers 503 for a
GitHub host whose token cannot be minted; it never forwards without a
credential. Verify the three credential classes:

```sh
sudo cluster-kubectl -n broker exec deploy/broker -- python -c '
import http.client
for path in ("healthz", "loki/loki/api/v1/labels", "github-platform/rate_limit"):
    c = http.client.HTTPConnection("127.0.0.1", 8080, timeout=20)
    c.putrequest("GET", "/" + path, skip_host=True)
    c.putheader("Host", "broker.broker.svc.cluster.local")
    c.endheaders()
    r = c.getresponse()
    print(path, r.status, r.read(140))
    c.close()
'
```

The explicit `Host` matters: a plain `urllib` call to `127.0.0.1` sends an
IP-literal Host, which the broker refuses with `400 ip_literal_host`.

Expected: `200`; `200` with the Loki gateway's `{"status":"success"...}`; and
`200` with the installation's rate limit. A `503` (`credential_unavailable`) on
`rate_limit` means the app id, private key or installation id is wrong: the
broker refuses rather than forwarding without a credential, so a 401 never
comes from here.

Rotation: generate a new key in the app settings, delete the Secret, repeat this
step (credentials are read once at start). `github-installation-id` is shared by
all three GitHub enrollments; their difference is the minted scope.

## 11. Rotate the Loki gateway token

The value is read from the environment at process start, so rotation is a
delete-and-roll, not a hot reload:

```sh
kubectl -n observability delete secret loki-gateway-token
# the ExternalSecret recreates it from the Password generator
kubectl -n observability rollout restart deployment/loki-gateway
```

Then repeat step 10 so the broker holds the new value, and query the `data`
tenant through the gateway to confirm (docs/observability.md, "Verification").

## 12. Create the cluster's SOPS-contract Secrets

Five cluster workloads read a credential that Cloudflare, Neuralwatt or the
operator issued, and none may exist in git. The operator creates each Secret
with `sops`; same rule as the host's `/run/secrets`: ciphertext lives in the
operator's secret store, plaintext only in the API server and the pod. The ESO
`kubernetes` provider projects four of them; OPDShelf reads its admin password
from the contract Secret directly, since it is a static value with no property
mapping.

Create the cluster tunnel (step 15), or take its token from the T7 work
(`smores56/sdlc#581`; `smores56/sdlc#580` depends on it), and write them all
encrypted with the same
age key the host secrets use:

```sh
# graphiti: Neuralwatt key for extraction and embeddings
sops -e -i neuralwatt-api-key.yaml
# ingress: the tunnel token from T7
sops -e -i cloudflare-tunnel-token.yaml
# calibre: OPDShelf's admin password (also the readers' Basic auth credential)
sops -e -i opdshelf-admin-password.yaml
# graphiti: the MCP wrapper's bearer token, the same value as the host secrets'
# memory-mcp-token in smores56/nix-config
sops -e -i memory-mcp-token.yaml
```

Then materialise the Secrets (SOPS contract names: `*-sops`; the broker's
`broker-credentials` is step 10 and is a plain Secret, not a `*-sops` one):

```sh
kubectl -n graphiti create secret generic neuralwatt-api-key-sops \
  --from-file=api-key=<(sops -d neuralwatt-api-key.yaml | yq -r .key | tr -d '\n')
kubectl -n ingress create secret generic cloudflare-tunnel-token-sops \
  --from-file=tunnel-token=<(sops -d cloudflare-tunnel-token.yaml | yq -r .token | tr -d '\n')
kubectl -n calibre create secret generic opdshelf-admin-password-sops \
  --from-file=password=<(sops -d opdshelf-admin-password.yaml | yq -r .password | tr -d '\n')
kubectl -n graphiti create secret generic memory-mcp-token-sops \
  --from-file=memory-mcp-token=<(sops -d memory-mcp-token.yaml | yq -r .token | tr -d '\n')
```

`tr -d '\n'` is load-bearing: `yq -r` ends its output with a newline and
`--from-file` stores it verbatim, which makes an `Authorization` header or a
Basic-auth password invalid. The graphiti renderer also strips line endings
defensively.

Resume the ESO stage (step 8) and confirm the projections, which is what the
Deployments actually read:

```sh
kubectl -n graphiti get secret neuralwatt-api-key -o jsonpath='{.data.api-key}' | base64 -d | wc -c
kubectl -n ingress get secret cloudflare-tunnel-token -o jsonpath='{.data.tunnel-token}' | base64 -d | wc -c
kubectl -n graphiti get secret memory-mcp-token -o jsonpath='{.data.token}' | base64 -d | wc -c
kubectl -n ingress get deploy cloudflared
```

Rotation is "rewrite the `*-sops` Secret, wait one refresh interval (1h), roll the
Deployment"; a lost key is a Neuralwatt/Cloudflare reissue, not a cluster
restore.

## 13. Cut the data apps over

Order, per-app checks and rollback are in `docs/data-migration.md`. Before the
first cutover:

1. Install the cluster tunnel's Cloudflare objects (T7,
   `smores56/sdlc#581`): the tunnel itself (step 15), then its DNS and Access
   objects from `tofu/` by adding the hostname to `cluster_hosts` and applying.
   The host tunnel keeps `ssh`, `agent`, `memory` and the Kanbei private route;
   `llm` and `chat` are retired by step 17. Until a hostname's cutover the
   wildcard record keeps it resolving to the host.
2. Confirm the storage contract bound:

   ```sh
   cluster-kubectl get nodes -o wide            # node name is exactly: cluster
   cluster-kubectl get pv                         # four PVs, STATUS Bound
   cluster-kubectl -n immich get pvc              # immich-data Bound
   ```

3. Create the OPDShelf admin password contract before exposing the route
   (`opdshelf-admin-password-sops`, step 12); the name is `smores56` and the
   password is also the readers' Basic auth credential.
4. The agent's write path is a file, not an API: OPDShelf's folder is the
   catalog, so `fanficfare-download` drops the EPUB where the host can move it
   into the share and `calibredb` loses its content-server tail. That change
   lives in `nix-config`, not here.
5. **Publish and pin the wrapper image.** In `smores56/nix-config`:
   `just publish-memory-image <git-sha>` builds the layered image, pushes it to
   `ghcr.io/smores56/nix-config/memory-mcp:<git-sha>` and prints the digest.
   Replace both `@sha256:000…` placeholders in
   `clusters/homelab/data/graphiti/{memory-mcp,memory-export-cronjob}.yaml` with
   it, and make the GHCR package public (Packages -> memory-mcp -> Change
   visibility); the cluster has no image pull secret.
6. **Move the wrapper.** Create `memory-mcp-token-sops` (step 12). Export
   **while the host stack still runs**: the graph snapshot is a `BGSAVE`
   through the live FalkorDB, so an export taken after the stop has no
   `graph.rdb` and exits non-zero. Write it into the share the cluster already
   mounts:

   ```sh
   sudo memory-export export \
     --root /var/lib/data/graphiti/db-dumps/memory/cutover \
     --state-dir /var/lib/memory \
     --graph-url redis://127.0.0.1:6379 \
     --graph-dir /var/lib/memory/falkordb \
     --graph-timeout 120
   sudo chown -R 1000:1000 /var/lib/data/graphiti/db-dumps/memory/cutover
   ```

   The restore Job runs as uid 1000, and SQLite's read-only open of a WAL
   registry still creates the `registry.sqlite-shm` sidecar next to it, so the
   pod must own the export directory; a root-owned 0755 tree fails with
   `attempt to write a readonly database`.

   Then stop the host stack
   (`sudo systemctl stop memory-export.timer; sudo systemctl stop memory-mcp
   memory-graphiti-mcp memory-llm-proxy memory-falkordb`) and scale
   `deploy/graphiti-falkordb` to 0. Install that export with the restore Job
   (*Restoring the memory registry*, directory
   `/data/db-dumps/memory/cutover`), then merge the platform branch so Flux
   creates the wrapper against the restored state. Set
   `memoryForward.enable = true` in nix-config (which also disables the host
   units declaratively, so a reboot cannot resurrect them), rebuild, and verify
   `memory status`, one write and one search. If the graph and registry still
   disagree, `memory reindex --all` until it reports nothing left.
   Keep the host state tree untouched for the observation week.
7. Leave `modules/nixos/{immich,navidrome,memory}.nix` in place until every app
   has served from the cluster for a week; then enable
   `clusterDataApps.enable` in nix-config (`docs/kanbei-split.md` in that
   repository) and, after that has run, delete the modules.

### Restoring the memory registry

There is no systemd in the cluster, so the restore is a Job. The directory
argument is the export being installed on the `graphiti-data` share: the
CronJob writes `/data/db-dumps/memory`, and the cutover export is
`/data/db-dumps/memory/cutover`. The directory must be owned by uid 1000 (the
Job's user): SQLite's read-only open of a WAL registry creates a
`registry.sqlite-shm` sidecar beside it.

```sh
# 1. Every writer and the graph stop first: the registry is single-writer.
#    On the first cutover `memory-mcp` does not exist yet; skip that line.
cluster-kubectl -n graphiti scale deploy/memory-mcp --replicas=0
cluster-kubectl -n graphiti scale deploy/graphiti-falkordb --replicas=0

# 2. Verify first, then write, from the same image the wrapper runs. Deleting
#    the job makes the command repeatable: a Job's template is immutable.
#    The pod template must carry `homelabs.dev/managed-by: platform`: the
#    admission policy (platform/policies/08-data-workload-source.yaml) denies a
#    job-created pod without it, so the Job stays 0/1 with FailedCreate events.
cluster-kubectl -n graphiti delete job memory-restore --ignore-not-found
cluster-kubectl -n graphiti create job memory-restore --image=<the pinned digest> \
  --dry-run=client -o yaml \
  | yq '.spec.ttlSecondsAfterFinished = 86400 | .spec.template.metadata.labels = {"homelabs.dev/managed-by": "platform", "homelabs.dev/tier": "data"} | .spec.template.spec = {"securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": true, "seccompProfile": {"type": "RuntimeDefault"}, "restartPolicy": "Never", "serviceAccountName": "data-workload", "automountServiceAccountToken": false}, "containers": [{"name": "restore", "image": "<the pinned digest>", "command": ["memory-export", "restore", "/data/db-dumps/memory", "--state-dir", "/data/memory", "--graph-dir", "/data/falkordb", "--owner", "", "--no-stop"], "securityContext": {"allowPrivilegeEscalation": false, "readOnlyRootFilesystem": true, "capabilities": {"drop": ["ALL"]}}, "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}}, "volumeMounts": [{"name": "data", "mountPath": "/data"}, {"name": "tmp", "mountPath": "/tmp"}]}], "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "graphiti-data"}}, {"name": "tmp", "emptyDir": {}}]}' \
  | cluster-kubectl -n graphiti apply -f -
cluster-kubectl -n graphiti logs -f job/memory-restore

# 3. A restore writes `/data/memory/.restore-in-progress` and removes it only on
#    success; while it exists every export refuses to run. After an interrupted
#    restore, reconcile state, then delete it with the same Job shape and the
#    command `rm /data/memory/.restore-in-progress`.

# 4. Boot FalkorDB once with the AOF off so the restored dump.rdb loads. Redis
#    with `appendonly yes` starts empty when no AOF exists; renaming the old one
#    is necessary but not sufficient (see the note after this block).
cluster-kubectl -n graphiti patch deploy graphiti-falkordb --type=strategic \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"falkordb","env":[{"name":"REDIS_ARGS","value":"--appendonly no --save \"\""}]}]}}}}'
cluster-kubectl -n graphiti scale deploy/graphiti-falkordb --replicas=1
# wait until the log says "DB loaded from disk" and this count is non-zero:
cluster-kubectl -n graphiti exec deploy/graphiti-falkordb -- \
  redis-cli GRAPH.QUERY global "MATCH (n) RETURN count(n)"
cluster-kubectl -n graphiti exec deploy/graphiti-falkordb -- \
  redis-cli CONFIG SET appendonly yes
# back to the deployed args; the Recreate reloads the AOF this just wrote
cluster-kubectl -n graphiti patch deploy graphiti-falkordb --type=strategic \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"falkordb","env":[{"name":"REDIS_ARGS","value":"--appendonly yes --appendfsync everysec --save \"\""}]}]}}}}'
cluster-kubectl -n graphiti scale deploy/memory-mcp --replicas=1
```

The two `REDIS_ARGS` patches fight Flux: the data stage re-applies the
Deployment within its 10-minute interval, so run the block in one go. If the
node count stays 0, stop before `CONFIG SET` — the snapshot did not load.

Rolling back to the host is `sudo memory restore <export>` on smortress after
copying the export out of the share — not a restart of the frozen host copy,
which is missing every write since the cutover.

## 14. Move the Cloudflare OpenTofu state

The config moved from `nix-config/tofu/` to `tofu/` here (SDLC #581) with the
resource addresses unchanged, so the state file carries over rather than being
re-imported. State is never committed.

```sh
# on smortress, as the operator
cd ~/src/homelabs-platform
sudo cp /var/lib/cloudflare-tofu/terraform.tfstate tofu/terraform.tfstate
sudo chown "$(id -u):$(id -g)" tofu/terraform.tfstate
```

Export the variables from `docs/cloudflare.md` ("Applying"), then:

```sh
tofu -chdir=tofu init -input=false
tofu -chdir=tofu plan -no-color
```

Expected: no replacements. The diff is the new wildcard application, the four
public bypass applications, the two CAA records, and an in-place update of the
four older applications from `domain` to `destinations`. Anything else — in
particular `-/+` on an Access application or the wildcard DNS record — means the
state does not match; stop before applying and compare IDs.

```sh
tofu -chdir=tofu apply -no-color
```

Then keep the state file with the rest of the operator credentials and delete
the old copy once a second plan is clean:

```sh
sudo rm -rf /var/lib/cloudflare-tofu
```

The nix-config flake apps `cloudflare-plan` / `cloudflare-apply` are removed with
this migration; do not run them after the move, because they copy the old `.tf`
files over the state.

Adopting an account whose state was lost needs imports instead: `<zone_id>/<record_id>`
for records and `accounts/<account_id>/<app_id>` for applications, listed from
`/zones/<zone>/dns_records` and `/accounts/<account>/access/apps`.

### Moving the state into R2 (SDLC #622)

The config now carries a `backend "s3" {}` block pointed at R2
(`tofu/backend.tf`), so the operator's local file has to become that bucket's
object. This is the destructive-looking step: an `init` against an empty bucket
and a local file that was forgotten would plan a create for every object that
already exists. Do it in this order and read the `state list` diff.

1. Create the bucket and its key. In the Cloudflare dashboard: R2 → **Create
   bucket** → `homelabs-tofu-state` (private, default location), then R2 → **Manage
   API tokens** → **Create API token** → *Object Read & Write* on that bucket
   only. Store the pair in the `cloudflare` environment:

   ```sh
   gh secret set R2_STATE_ACCESS_KEY_ID --env cloudflare --body "<access key id>"
   gh secret set R2_STATE_SECRET_ACCESS_KEY --env cloudflare --body "<secret access key>"
   ```

2. Back up the local state and record what it holds, before touching anything:

   ```sh
   cd ~/src/homelabs-platform
   cp tofu/terraform.tfstate "tofu/terraform.tfstate.pre-r2-$(date +%F)"
   ./scripts/tofu-cloudflare.sh state pull > "/var/backups/tofu-state-$(date +%F).json" 2>/dev/null \
     || tofu -chdir=tofu state pull > "/var/backups/tofu-state-$(date +%F).json"
   ./scripts/tofu-cloudflare.sh state list | sort > /tmp/tofu-state-before.txt
   wc -l /tmp/tofu-state-before.txt        # record the number
   ```

   Keep both copies off the checkout: the JSON is the object graph, the `.pre-r2`
   file is what `-migrate-state` reads.

3. Migrate. `init` asks whether to copy the existing state into the new backend:

   ```sh
   ./scripts/tofu-cloudflare.sh init -migrate-state
   ```

   Answer **yes**. The script supplies the R2 pair from SOPS; `AWS_DEFAULT_REGION`
   is forced to `auto`. A `403` here means the token is not scoped to the bucket;
   an `AccessDenied` on `HeadBucket` means the bucket name in `tofu/backend.tf`
   does not exist yet.

4. Prove the state moved unchanged:

   ```sh
   ./scripts/tofu-cloudflare.sh state list | sort > /tmp/tofu-state-after.txt
   diff -u /tmp/tofu-state-before.txt /tmp/tofu-state-after.txt   # empty
   ./scripts/tofu-cloudflare.sh plan -no-color | tail -20          # no changes
   ```

   A non-empty diff or a plan that proposes a create means the migration did not
   carry the state: restore `tofu/terraform.tfstate` from step 2 and do not
   continue.

5. Remove the local copies so nothing can plan from them, then confirm CI reads
   the same state. Dispatch `tofu-apply` with `apply` left false and check that
   its plan is empty:

   ```sh
   rm tofu/terraform.tfstate tofu/terraform.tfstate.backup
   gh workflow run tofu-apply --repo smores56/homelabs-platform
   gh run watch --repo smores56/homelabs-platform
   ```

   The workflow's summary shows the plan. From here on, an apply is
   `workflow_dispatch` with `apply: true` after a reviewed plan-only run — never a
   push. `tofu-plan.yml` on main is the drift check.

6. Rollback, if the bucket has to be abandoned: `tofu init -migrate-state` with
   `backend.tf` reverted to the local default copies the object back into
   `tofu/terraform.tfstate`. Do that before deleting the bucket, not after.

## 15. Create the cluster tunnel

The k3s connector (`clusters/homelab/ingress/`) has no token and stays
unstarted until this exists.

1. Zero Trust → Networks → Tunnels → **Create a tunnel** → `cloudflared`, name
   `cluster`. Add **no** public hostname: DNS and Access come from `tofu/`.
2. Copy the token into the SOPS contract `cloudflare-tunnel-token-sops` in the
   `ingress` namespace (step 12). The token never enters git.
3. Record the tunnel UUID in `tofu/terraform.tfvars` (`cluster_tunnel_id`) and
   commit it: the apply reads every identifier from that file.

4. At each app's cutover, add its origin to `local.cluster_ingress` in
   `tofu/cluster.tf` and apply. DNS follows the map, so the record and the
   tunnel rule cannot drift:

```sh
tofu -chdir=tofu apply -no-color
dig +short photos.sammohr.dev
```

Expected: the CNAME points at `<cluster-uuid>.cfargotunnel.com` instead of the
host tunnel, and `kubectl -n ingress get deploy cloudflared` shows two ready
replicas. Deleting the entry from `local.cluster_ingress` flips that name back
to the host tunnel and is the rollback.

## 16. Capture the ACME account id for the CAA pin

`tofu/` pins `issue` and `issuewild` to the ACME account Kanbei's in-VM CSR
client registers with. Nothing blocks earlier applies: `acme_account_id`
defaults to empty and the two CAA records are created only once it is set, so
until this client has registered the zone simply has no CAA pin.

The responder exists only while the Kanbei VM is enabled (`kanbeiVm.enable`,
which waits on the GPU cutover): until then `kanbei-acme.service` is not defined,
`/var/lib/kanbei-acme` does not exist, and an empty `acme_account_id` is the
correct value. Re-run this step after that VM first requests a certificate.

The account URL the client prints on first registration ends in
`/acme/acct/<id>`:

```
Account URL: https://acme-v02.api.letsencrypt.org/acme/acct/<id>
```

Read it from that client's first-registration output or its persisted state;
registering the same account key again returns the same URL, so a lost log
costs a re-registration, not a new account. That trailing number goes into `acme_account_id` in `tofu/terraform.tfvars`:

```sh
tofu -chdir=tofu plan -no-color   # two CAA records to add
tofu -chdir=tofu apply -no-color
```

Then confirm what the world sees:

```sh
dig +short CAA sammohr.dev
```

Expected: the two pinned records (`issue` and `issuewild`, each with
`accounturi=https://acme-v02.api.letsencrypt.org/acme/acct/<id>`) **and** the
records Cloudflare appends for Universal SSL (`pki.goog`, `letsencrypt.org`,
`ssl.com`, `sectigo.com`). Those are added because the zone now has CAA records
and are not visible in the dashboard; `docs/cloudflare.md` explains why the pin
narrows but does not lock down issuance.

## 17. Retire `llm.sammohr.dev` and `chat.sammohr.dev`

Both applications carried `prevent_destroy`, so dropping the block cannot delete
them. Remove the state entry first, then the object:

```sh
tofu -chdir=tofu state rm cloudflare_zero_trust_access_application.llm
tofu -chdir=tofu state rm cloudflare_zero_trust_access_application.chat
```

Then in Zero Trust → Access → Applications delete the `llm` and `chat`
applications by hand, and confirm the state holds neither:

```sh
tofu -chdir=tofu state list | grep -E 'access_application\.(llm|chat)$' || echo "retired"
```

`webProxy.services` in nix-config loses both names in the change that removes
`tofu/`; until the host rebuild runs, the tunnel ingress still lists them, but
the wildcard application denies the hostnames, so the origin is unreachable:

```sh
curl -sS -o /dev/null -D - https://llm.sammohr.dev/ | head -3
```

Expected: `HTTP/2 302` with a `location:` on `*.cloudflareaccess.com`, never a
response from the host.

## 18. Prove the Access default-deny

The static half runs in CI; this is the live half, and it needs the token:

```sh
CLOUDFLARE_API_TOKEN=... tests/access/check.sh
```

Expected output is in `docs/cloudflare.md` ("Verifying"). The script asks the
API which application matches a fresh hostname (it must be the wildcard) and
then makes a real request: a `302` to the Access login is a pass, an answer from
the origin is a failure. Re-run it after any Access change; run it with
`ACCESS_PROBE_HOST=name.sammohr.dev` to check a specific hostname instead.

## 19. Publish the broker image and pin its digest

`apps/broker/` is the source and `clusters/homelab/broker/deployment.yaml` ships
a deliberate all-zero digest placeholder. From a machine with registry access:

```sh
docker build -t ghcr.io/smores56/homelabs-platform/broker:<git-sha> apps/broker
docker push ghcr.io/smores56/homelabs-platform/broker:<git-sha>
nix run nixpkgs#crane -- digest ghcr.io/smores56/homelabs-platform/broker:<git-sha>
```

Replace the `image:` line with
`ghcr.io/smores56/homelabs-platform/broker@sha256:<digest>`, delete the
`PLACEHOLDER DIGEST` comment, and run `scripts/kustomize-build-all.sh` and
`tests/policies/check.sh` before pushing. The Dockerfile's base tag
(`python:3.13-slim`) should be replaced with its digest in the same change. Normally `.github/workflows/publish-broker.yml` does all of this on a push to
`main` that touches `apps/broker/**` (or on manual dispatch): it builds, pushes
`sha-<first 12 of the commit>`, and prints both the tag and the digest in the
run summary. Use the manual commands above only when Actions is unavailable.
The image installs one pinned dependency, `cryptography==44.0.0` (RS256 JWT
signing); bump it deliberately with the image digest.

Verify: `kubectl -n broker get pods` shows two `Running` replicas on
`runsc`, and `kubectl -n broker get pod -o
jsonpath='{.items[0].spec.containers[0].image}'` prints the digest.

## 20. Route `agent.sammohr.dev` and retire `nanobot.sammohr.dev`

The hostname moves from the host tunnel to the cluster tunnel, behind the
existing Access application `agent` (owner email plus service auth for maki).
No Cloudflare credential goes into the agent pod.

**Order matters.** This change removes the `nanobot` Access application from
`tofu/access.tf`. It carried `prevent_destroy`, so the state entry is removed
before the first apply; applying first fails on the destroy.

1. Drop the retired object from state (the object still exists at the edge):

   ```sh
   tofu -chdir=tofu state rm cloudflare_zero_trust_access_application.nanobot
   ```

2. Set the cluster contract and apply. `tofu/cluster.tf` owns the tunnel's
   hostname config and refuses a `cluster_hosts` entry with no origin rule:

   ```sh
   tofu -chdir=tofu plan -no-color
   # expect: the agent CNAME, cloudflare_zero_trust_tunnel_cloudflared_config.cluster
   # with agent -> http://nanobot.tenant-apps.svc.cluster.local:80, the `agent`
   # Access application unchanged, and no `nanobot` application in the plan
   tofu -chdir=tofu apply -no-color
   dig +short agent.sammohr.dev      # CNAME <cluster-uuid>.cfargotunnel.com
   ```

   Do not add the public hostname in the Zero Trust dashboard. The
   `cloudflare_zero_trust_tunnel_cloudflared_config.cluster` resource is the
   source of truth; a dashboard edit is replaced by the next apply.

3. Mint the maki service token in Zero Trust → Access → Service Auth. The
   `service auth` policy already accepts any valid service token; store the
   `CF-Access-Client-Id`/`CF-Access-Client-Secret` pair in maki's client config,
   never in the cluster. The owner uses the browser identity.

4. Delete the `nanobot` application at the edge: Zero Trust → Access →
   Applications → `nanobot` → Delete. Its state entry is already gone, so a
   later apply neither recreates nor destroys it.

5. Verify the edge and the pod:

   ```sh
   curl -sS -o /dev/null -D - https://agent.sammohr.dev/ | head -3     # 302 to *.cloudflareaccess.com
   curl -sS -o /dev/null -D - https://nanobot.sammohr.dev/ | head -3   # 302 to Access, never an origin
   cluster-kubectl -n tenant-apps get deploy nanobot
   cluster-kubectl -n tenant-apps get pods -l app.kubernetes.io/name=nanobot -o wide
   cluster-kubectl -n tenant-apps exec deploy/nanobot -- env | grep -E 'BROKER_URL|CLOUDFLARE' || true
   ```

   Expected: `BROKER_URL` is set, no `CLOUDFLARE_*` variable exists, and the
   pod has no mounted service-account token.

### Rotating the agent's WebUI token

The WebUI password is `channels.websocket.tokenIssueSecret` in
`$HOME/.nanobot/config.json` on the scratch PVC
(`/data/home/.nanobot/config.json`). nanobot generates it on first start, so it
appears in that bootstrap's pod log; rotate it if the log was shared. Rotation
invalidates every issued websocket token, so open browsers must sign in again.

```sh
cluster-kubectl -n tenant-apps exec -i deploy/nanobot -- python - <<'PY'
import json, secrets, pathlib
p = pathlib.Path("/data/home/.nanobot/config.json")
c = json.loads(p.read_text())
ws = c.setdefault("channels", {}).setdefault("websocket", {})
ws["tokenIssueSecret"] = secrets.token_hex(16)
p.write_text(json.dumps(c, indent=2) + "\n")
print("new WebUI token:", ws["tokenIssueSecret"])
PY
cluster-kubectl -n tenant-apps rollout restart deploy/nanobot
```

`render.py` rewrites only the listener hosts, providers, presets, prompt, skills
and cron, so the rotated secret survives restarts; the rollout also discards the
old container log. Store the printed token in the password manager.

## 21. Kanbei public hostname behind Access

`tofu/kanbei.tf` creates the Access application for `kanbei.sammohr.dev`. The
hostname is published by the host tunnel's ingress in nix-config
(`webProxy.services.kanbei` -> `https://127.0.0.1:443`, the relay into the
guest), so the guest's certificate is what cloudflared verifies between the
connector and the origin. The wildcard CNAME in `tofu/main.tf` resolves the
name; no `cloudflare_dns_record` is created, and `tests/tofu/check.sh` asserts
that. A browser sees the Cloudflare edge certificate, not the guest's.

This change retires the private WARP hostname route, and that takes two applies.
OpenTofu refuses to destroy a resource whose state still carries
`prevent_destroy`, so the route's guard is dropped first and the follow-up
change deletes the resource.

1. Publish the ingress first, in nix-config: the same generation enables
   `kanbeiVm` and declares `webProxy.services.kanbei`. After the switch:

   ```sh
   curl -sS -o /dev/null -D - https://kanbei.sammohr.dev/ | head -3
   # expect: HTTP/2 302 to <team>.cloudflareaccess.com
   ```

2. Apply the application change; the plan must show no DNS record:

   ```sh
   tofu -chdir=tofu plan -no-color
   # expect: cloudflare_zero_trust_access_application.kanbei (update), nothing else
   tofu -chdir=tofu apply -no-color
   ```

3. Apply the route removal, in the commit that deletes the resource:

   ```sh
   tofu -chdir=tofu plan -no-color
   # expect: cloudflare_zero_trust_network_hostname_route.kanbei (destroy), nothing else
   tofu -chdir=tofu apply -no-color
   ```

4. Verify the name is public-but-gated and no private route survives:

   ```sh
   tofu -chdir=tofu state list | grep -E 'network_hostname_route|access_application\.kanbei$'
   dig +short kanbei.sammohr.dev      # Cloudflare edge addresses, never 172.64.128.0/20
   ```

   A token address from the Gateway initial-resolved range means a hostname
   route still exists, and WARP clients would take the retired path.

5. The application allows the owner email and **nothing else**. It deliberately
   does not attach the account-wide `Service Auth` policy: that decision accepts
   any valid service token in the account, so the agent's own token would open
   Kanbei. Do not attach it; an unattended client that must reach Kanbei needs
   its own Access decision plus a reviewed terraform change. `tests/tofu/check.sh`
   fails if a second policy (or a bypass) appears on the application.

## 22. Create the read-only mirror PAT and run `github-backup`

The CronJob in `clusters/homelab/platform/backup/` mirrors
`smores56/homelabs-platform` and `smores56/homelabs-apps` into a bundle on the
`github-backup-data` PVC. Its only credential is a fine-grained read-only PAT;
the script refuses a classic `ghp_` token at start.

1. Mint a fine-grained PAT (GitHub → Settings → Developer settings →
   Fine-grained tokens → Generate new token):

   - Resource owner: `smores56`.
   - Repository access: **Only select repositories** → `homelabs-platform`,
     `homelabs-apps`.
   - Permissions: **Contents: Read-only**; `Metadata: Read-only` is added
     automatically and is the only other permission. Nothing else — no
     Administration, no Pull requests, no Workflows.
   - Expiration: 90 days.

2. Store it as a SOPS contract Secret. `github-backup-read-pat.yaml` holds one
   key, `token`, and is encrypted with the operator's key:

   ```sh
   sops -e -i github-backup-read-pat.yaml
   kubectl -n platform create secret generic github-backup-read-pat-sops \
     --from-file=token=<(sops -d github-backup-read-pat.yaml | yq -r .token | tr -d '\n')
   ```

3. Resume the ESO stage (step 8) if it is not already running, then verify the
   projection without printing the value:

   ```sh
   kubectl -n platform get secret github-backup-read-pat -o jsonpath='{.data.token}' |
     base64 -d | cut -c1-12        # expect github_pat_
   ```

4. Run the job once and read its log. The log names the commit, ref count and
   bundle size; a missing bundle for an empty repository is reported as
   `commit=none` rather than a silent success.

   ```sh
   kubectl -n platform create job --from=cronjob/github-backup github-backup-manual
   kubectl -n platform logs job/github-backup-manual -f
   kubectl -n platform get pvc github-backup-data    # Bound
   ```

   Restore check: copy the bundle out of a completed job pod before its
   24-hour TTL expires and clone it offline.

   ```sh
   pod=$(kubectl -n platform get pods -l job-name=github-backup-manual -o name | head -1)
   kubectl -n platform cp "$pod:/backup/bundles/smores56/homelabs-platform.bundle" .
   git clone homelabs-platform.bundle /tmp/restore-check
   ```

5. Revoke the classic write PAT the old host-side mirror used. The cluster job
   never needs one: it clones over HTTPS with `x-access-token` and the
   fine-grained token. GitHub → Settings → Developer settings → Personal access
   tokens → **Tokens (classic)** → delete the token with `repo` scope. Confirm
   the list is empty (or holds only unrelated tokens) before recording the step
   as done.

6. The nightly run is `15 4 * * *` Europe/Berlin, after the host's restic walk.
   Rotation is: mint the replacement in step 1, repeat step 2, wait for the 1h
   ESO refresh (or delete the projected Secret so it is recreated), then revoke
   the old PAT.

## 23. Verify the digest job, the share and the host writer hand-off

The CronJob in `clusters/homelab/platform/digest/` writes `latest.json` and 96
snapshots to `/mnt/data/cluster-digest`; the host reads the same tree at
`/var/lib/data/cluster-digest` and age-encrypts each new document into Kanbei's
inbox. The cluster holds no age key.

1. Land the share in nix-config. The guest module must list the share, or the
   PVC stays Pending and nothing is written:

   ```nix
   # modules/nixos/cluster-vm.nix, the dataShares list (lines 25-30)
   dataShares = [
     "immich"
     "music"
     "books"
     "graphiti"
     "cluster-digest"
   ];
   ```

   Rebuild the `cluster` guest, then confirm the host directory exists with the
   owner the guest translation maps to (uid 1000):

   ```sh
   ls -ld /var/lib/data/cluster-digest
   cluster-kubectl get nodes -o wide          # node name must be `cluster`
   ```

2. Confirm the static volume and the claim bound to it:

   ```sh
   kubectl get pv cluster-digest-share        # Bound, cluster-digest-data
   kubectl -n observability get pvc cluster-digest-data   # Bound
   ```

   A Pending claim means step 1 is missing or the node name is not `cluster`;
   the PV's node affinity fails closed rather than binding elsewhere.

3. Run the job once and read its log. The builder prints one JSON summary line
   (`generated_at`, `bytes`, `events`, `pruned`) and exits 1 when either source
   failed, so a failed read is visible without opening the document:

   ```sh
   kubectl -n observability create job --from=cronjob/cluster-digest cluster-digest-manual
   kubectl -n observability logs job/cluster-digest-manual -f
   ```

4. Read the document on the host. `jq` shows the schema version, a fresh UTC
   `generated_at`, and an event count at or below 400; `wc -c` must stay at or
   below 65536. Both freshness entries carry a *measured* age, not a constant:

   ```sh
   jq -r '.schema_version, .generated_at, .source_freshness,
     (.nanobot_activity.events|length)' /var/lib/data/cluster-digest/latest.json
   wc -c /var/lib/data/cluster-digest/latest.json
   ls /var/lib/data/cluster-digest | wc -l    # latest.json + at most 96 snapshots
   ```

   - `status_exporter.age_seconds` is `generated_at` minus the exporter's
     `last_scrape_timestamp_seconds`, so it is under 90 s when the exporter is
     refreshing. `ok: false` means the exporter was unreachable or its last
     scrape failed, and the job exits 1.
   - `loki_gateway.age_seconds` is `generated_at` minus the newest stored
     data-tenant entry. `age_seconds: null` with `stale: true` means the 15 m
     window held no data-tenant line at all: the Vector write path is broken
     (or nothing logged), and the job logs `digest: stale source loki_gateway`
     on stderr while still writing the document. Do not read an empty
     `nanobot_activity` as a quiet host unless `stale` is false.

   The schedule is `*/15 * * * *` UTC, so `generated_at` should be no more than
   15 minutes old at any time. A stale `generated_at` means the CronJob is not
   running; a fresh `generated_at` with `ok: false` means a source is down.

5. Confirm the host writer's half. It watches only `latest.json` (snapshots are
   local debugging history), uses `generated_at` as a cursor so a restart does
   not duplicate a batch, and drops age ciphertext into `/var/lib/data/inbox`
   (T14). A new file in the inbox starting with `age-encryption.org/v1` is the
   successful end-to-end:

   ```sh
   ls -t /var/lib/data/inbox | head -1
   head -c 30 /var/lib/data/inbox/"$(ls -t /var/lib/data/inbox | head -1)"; echo
   ```

6. Confirm there is no way back. No Service selects the digest pod and no
   ingress policy opens it to another namespace:

   ```sh
   kubectl -n observability get svc -l app.kubernetes.io/name=cluster-digest   # empty
   kubectl -n observability get networkpolicy cluster-digest-egress \
     -o jsonpath='{.spec.policyTypes}{"\n"}{.spec.ingress}{"\n"}'             # Egress,Ingress / nothing
   ```

## 24. Give each tree its restic Secret, then run the drill

Four trees now back themselves up from the cluster (`clusters/homelab/data/*/restic-backup.yaml`,
SDLC #619): `immich`, `navidrome`, `calibre` and `graphiti`, each with its own
CronJob, its own claim mounted read-only and its own repository at
`s3:https://<account>.r2.cloudflarestorage.com/restic-backup/trees/<tree>`. The
host's `restic-backups-r2.timer` keeps running and stays the owner of every tree
until that tree's drill below passes; nothing in this repository stops it.

1. Create one SOPS Secret per namespace. The R2 key pair is the host's existing
   one (`r2-access-key-id` / `r2-secret-access-key` in nix-config's
   `secrets/smortress.yaml`); `restic-password` is the password the host's
   repository already uses, so the operator can read both repositories with the
   same key. Encrypt the file with the operator's age key and never commit it,
   then:

   ```sh
   for ns in immich navidrome calibre graphiti; do
     kubectl -n "$ns" create secret generic restic-backup-sops \
       --from-file=r2-access-key-id --from-file=r2-secret-access-key \
       --from-file=restic-password
   done
   ```

   `ExternalSecret` `restic-credentials` in each namespace projects it with the
   key names the jobs read (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
   `RESTIC_PASSWORD`). The `platform-secrets-eso` stage must be resumed first
   (step 8), or the two CronJobs stay unschedulable on a missing Secret.

2. Run the preflight for each tree. It proves read access as uid 1000, creates
   the repository if it does not exist yet, then reads back a metadata check and
   100 MiB of data:

   ```sh
   for ns in immich navidrome calibre graphiti; do
     kubectl -n "$ns" create job --from=cronjob/restic-preflight "preflight-$(date +%s)"
   done
   kubectl -n immich get jobs -w        # then the other three namespaces
   kubectl -n immich logs job/<name>
   ```

   A job that lists unreadable paths means that tree is not yet safe as uid 1000:
   leave it on the host timer and fix the ownership on the share
   (`sudo chown -R smores:users /var/lib/data/<tree>` from the host, which is the
   uid 1000 mapping). Do not `chmod -R` a live database directory.

3. Run one backup and confirm the snapshot exists:

   ```sh
   kubectl -n immich create job --from=cronjob/restic-backup "restic-$(date +%s)"
   kubectl -n immich logs job/<name>
   kubectl -n immich get cronjob restic-backup -o jsonpath='{.status.lastSuccessfulTime}{"\n"}'
   ```

4. Run the drill from a workstation. The exact commands are the `drill` key of
   the tree's `restic-contract` ConfigMap, and the preflight prints the same text
   on every run, so the two cannot disagree:

   ```sh
   kubectl -n immich get configmap restic-contract -o jsonpath='{.data.drill}{"\n"}'
   ```

   The drill passes when `restic snapshots --host cluster-<tree>` shows a
   snapshot from the last night and the restored file opens. Only then does that
   tree leave the host timer — a separate change in nix-config that removes one
   tree from `restic-backups-r2`'s `paths`/`backupPrepareCommand`. A tree that
   fails the drill stays host-side; the cluster job may keep running beside it,
   since the two write different repositories.

5. Re-pin the restic image when upgrading. The job pins the 0.18.1 manifest list;
   the commands are the same as step 9:

   ```sh
   crane digest docker.io/restic/restic:0.18.1
   ```

The failure signal for a tree is the Alertmanager route in step 25
(`ResticBackupStale` fires when no successful run has been recorded for 26 hours)
— check `kubectl -n <ns> get jobs` and the Job's log before assuming the share is
the problem.

## 25. Create the Alertmanager mail token and send a test alert

Cluster alerts go Prometheus → Alertmanager → `mailrelay` → Cloudflare's Email
Service API (`clusters/homelab/observability/alertmanager/` and `mailrelay/`).
The relay needs one credential; the sender, recipient and account tag are in the
Deployment as identifiers.

1. Mint a Cloudflare API token with **Email Sending: Edit** on the account, on the
   same onboarding as the host's `cloudflare-email` sender (the from address must
   be on `send.sammohr.dev`, and the recipient must be verified there). Store it:

   ```sh
   kubectl -n observability create secret generic alertmanager-mail-sops \
     --from-literal=token=<token>
   ```

   Do not commit that file. `ExternalSecret` `mailrelay-token` projects it, and the
   `platform-observability-secrets` stage must be resumed first (step 8).

2. Confirm the whole chain before trusting it:

   ```sh
   kubectl -n observability get deploy alertmanager mailrelay
   kubectl -n observability port-forward svc/alertmanager 9093:9093 &
   curl -s -XPOST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{
     "labels": {"alertname": "MailPathTest", "severity": "warning", "namespace": "observability"},
     "annotations": {"summary": "manual test of the alert path"}}]'
   kubectl -n observability logs deploy/mailrelay --tail=20
   ```

   The relay log shows `"POST /alert HTTP/1.1" 200`; a 502 means Cloudflare
   rejected the call (bad token, unverified recipient, wrong from domain) and the
   response body names which. The test alert resolves after
   `global.resolve_timeout` (5m) and sends a second, resolved mail.

3. Only after the test mail arrives may a moved unit lose its `cloudflare-email@`
   hook. Host-only units keep it: there is no host → cluster ingest yet.

4. Rotating the token is: replace `alertmanager-mail-sops`, wait for the 1h ESO
   refresh (or delete `mailrelay-token`), then revoke the old token. The relay
   reads the mounted file per delivery, so nothing restarts.
