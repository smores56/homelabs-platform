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
still push a bootstrap commit, while the machine user and every other account
are bound by PR review and the required checks.

After applying, confirm the machine user cannot push to `main` (expected:
`protected branch hook declined`) and cannot force-push (expected: `Cannot
force-push to a protected branch`). Record both outputs beside the runbook.

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
  --url=ssh://git@github.com/smores56/homelabs-apps
```

Take the printed public key, add it to `smores56/homelabs-apps` → Settings →
Deploy keys → **Allow write access: off**, then verify:

```sh
flux -n tenant-apps get gitrepository homelabs-apps -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
```

Expected: `True`.

## 8. Install External Secrets and resume its stage

`platform-secrets-eso` is suspended because the operator is not shipped by this
repository; the stage would otherwise fail on missing CRDs and block its
dependents.

```sh
helm repo add external-secrets https://charts.external-secrets.io
helm repo update
helm install external-secrets external-secrets/external-secrets \
  --namespace platform --create-namespace
flux -n flux-system resume kustomization platform-secrets-eso
flux -n flux-system resume kustomization platform-observability-secrets
kubectl -n tenant-apps get externalsecret nanobot-broker-token
kubectl -n observability get externalsecret loki-gateway-token
```

Expected: both generated Secrets exist and are owned by their ExternalSecrets.
`loki-gateway-token` is what unblocks the Loki gateway and Vector's data sink;
until it exists both stay unstarted (fail closed). Rotate a token by deleting
its Secret and letting the `Password` generator mint a new one.

## 9. Digest-pin the observability images

The manifests pin version tags. Replace each tag with a digest once, from a
machine with registry access (the agent VM has none):

```sh
for ref in prom/prometheus:v3.16.0 grafana/loki:3.7.9 \
  timberio/vector:0.61.0-debian python:3.13.12-slim busybox:1.37.0 \
  ghcr.io/immich-app/immich-server:v3.0.3 \
  ghcr.io/immich-app/immich-machine-learning:v3.0.3 \
  ghcr.io/immich-app/postgres:17-vectorchord0.4.3-pgvector0.8.1 \
  deluan/navidrome:0.63.2 crocodilestick/calibre-web-automated:4.0.8 \
  cloudflare/cloudflared:2026.10.2; do
  printf '%s -> %s\n' "$ref" "$(crane digest "$ref")"
done
```

Then rewrite those `image:` lines under
`clusters/homelab/observability/{prometheus,loki,vector,status-exporter,loki-gateway}`,
`clusters/homelab/data/{immich,navidrome,calibre,graphiti}` and
`clusters/homelab/ingress`
to `repo@sha256:<digest>` (keep the tag when the registry allows
`repo:tag@sha256:...`), run `scripts/kustomize-build-all.sh` and
`tests/observability/check.sh`, and commit. The image-pin assertion in
`tests/observability/check.sh` accepts either a version tag or a `sha256:` digest,
so a half-finished sweep still fails CI rather than shipping `latest`.

## 10. Wire the broker's `loki` logical host

The broker is deployed from `homelabs-apps`; this repository only supplies the
gateway and the token. Copy the token into the broker's secret store (never into
git, never into this repository):

```sh
kubectl -n observability get secret loki-gateway-token \
  -o jsonpath='{.data.password}' | base64 -d
```

Configure the broker's logical host `loki` as
`http://loki-gateway.observability.svc.cluster.local:3100`, inject the value as
`Authorization: Bearer <token>`, and strip any `Authorization` the agent sends.
The full contract is in `docs/observability.md` ("Broker logical host `loki`").
Verify from a broker pod:

```sh
curl -sS -H "Authorization: Bearer $TOKEN" \
  --get http://loki-gateway.observability.svc.cluster.local:3100/loki/api/v1/labels
```

Expected: `{"status":"success",...}` listing `namespace` and `tier`. A request
without the header must answer 401, and a tenant-apps pod must not reach the
gateway at all (NetworkPolicy).

## 11. Rotate the Loki gateway token

The value is read from the environment at process start, so rotation is a
delete-and-roll, not a hot reload:

```sh
kubectl -n observability delete secret loki-gateway-token
# the ExternalSecret recreates it from the Password generator
kubectl -n observability rollout restart deployment/loki-gateway
kubectl -n observability rollout restart daemonset/vector
```

Then repeat step 10 so the broker holds the new value, and query the `data`
tenant through the gateway to confirm (docs/observability.md, "Verification").

## 12. Create the cluster's SOPS-contract Secrets

Two cluster workloads read a credential that Cloudflare or Neuralwatt issued, and
neither may exist in git: the ESO `kubernetes` provider projects them from a
Secret that the operator creates with `sops`. Same rule as the host's
`/run/secrets`: ciphertext lives in the operator's secret store, plaintext only
in the API server and the pod.

Create the cluster tunnel (step 15), or take its token from the T7 work
(`smores56/sdlc#581`; `smores56/sdlc#580` depends on it), and write both files
encrypted with the same
age key the host secrets use:

```sh
# graphiti: Neuralwatt key for extraction and embeddings
sops -e -i neuralwatt-api-key.yaml
# ingress: the tunnel token from T7
sops -e -i cloudflare-tunnel-token.yaml
```

Then materialise the two Secrets (SOPS contract names: `*-sops`):

```sh
kubectl -n graphiti create secret generic neuralwatt-api-key-sops \
  --from-file=api-key=<(sops -d neuralwatt-api-key.yaml | yq -r .key)
kubectl -n ingress create secret generic cloudflare-tunnel-token-sops \
  --from-file=tunnel-token=<(sops -d cloudflare-tunnel-token.yaml | yq -r .token)
```

Resume the ESO stage (step 8) and confirm the projections, which is what the
Deployments actually read:

```sh
kubectl -n graphiti get secret neuralwatt-api-key -o jsonpath='{.data.api-key}' | base64 -d | wc -c
kubectl -n ingress get secret cloudflare-tunnel-token -o jsonpath='{.data.tunnel-token}' | base64 -d | wc -c
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

3. After Immich serves from the cluster, set the Calibre-Web admin password on
   first login (`admin` / `admin123` is the image default and the route is
   public with app auth, so this step is required, not optional).
4. Repoint agent access to Calibre: the broker's `calibre` logical host and the
   agent `calibredb` wrapper expect calibre-server's content server; Calibre-Web
   serves OPDS at `/opds`. That change lives in `nix-config` and
   `homelabs-apps`, not here.
5. Leave `modules/nixos/{immich,navidrome,memory}.nix` in place until every app
   has served from the cluster for a week; then enable
   `clusterDataApps.enable` in nix-config (`docs/kanbei-split.md` in that
   repository) and, after that has run, delete the modules.

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

## 15. Create the cluster tunnel

The k3s connector (`clusters/homelab/ingress/`) has no token and stays
unstarted until this exists.

1. Zero Trust → Networks → Tunnels → **Create a tunnel** → `cloudflared`, name
   `cluster`. Add **no** public hostname: DNS and Access come from `tofu/`.
2. Copy the token into the SOPS contract `cloudflare-tunnel-token-sops` in the
   `ingress` namespace (step 12). The token never enters git.
3. Record the tunnel UUID and export it for the apply:

```sh
export TF_VAR_cluster_tunnel_id=<tunnel UUID>
```

4. At each app's cutover, add the hostname and apply:

```sh
export TF_VAR_cluster_hosts='["immich"]'
tofu -chdir=tofu apply -no-color
dig +short immich.sammohr.dev
```

Expected: the CNAME now points at `<cluster-uuid>.cfargotunnel.com` instead of
the host tunnel, and `kubectl -n ingress get deploy cloudflared` shows
two ready replicas. Removing the name from `cluster_hosts` flips it back to the
host tunnel and is the rollback.

## 16. Capture the ACME account id for the CAA pin

`tofu/` pins `issue` and `issuewild` to the ACME account Kanbei's in-VM CSR
client registers with. The account URL the client prints on first registration
ends in `/acme/acct/<id>`:

```
Account URL: https://acme-v02.api.letsencrypt.org/acme/acct/<id>
```

Read it from that client's first-registration output or its persisted state;
registering the same account key again returns the same URL, so a lost log
costs a re-registration, not a new account. That trailing number is the input:

```sh
export TF_VAR_acme_account_id=<id>
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
