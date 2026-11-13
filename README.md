# homelabs-platform

Human-owned cluster platform for `smortress`: Flux bootstrap, namespaces, RBAC,
admission policy, network policy, SOPS/ESO stores, data-bearing app manifests,
Cloudflare OpenTofu, and the cluster-side CI that guards them.

Agents (nanobot) never push here. They work in
[`homelabs-apps`](https://github.com/smores56/homelabs-apps) and propose platform
changes by pull request from a fork, so no PR workflow can read a secret.

## Layout

| Path | Contents |
| --- | --- |
| `clusters/homelab/kustomization.yaml` | Flux root build; controller patches live here so bootstrap cannot overwrite them |
| `clusters/homelab/flux-system/` | Install manifests, root sync objects, `gotk-patches/`, image automation (`image-automation/`), `bootstrap.sh` |
| `clusters/homelab/platform/` | Ordered stages: namespaces, RBAC, admission policy, network, secrets, storage, backup, digest, data apps, ingress, observability, nanobot policy, tenant reconciler |
| `clusters/homelab/data/` | Data-bearing apps, one namespace and one virtiofs share each: Immich, Navidrome, Calibre, Graphiti (`docs/data-migration.md`) |
| `clusters/homelab/ingress/` | `cloudflared` for the cluster tunnel; the host tunnel stays human-owned |
| `clusters/homelab/observability/` | Prometheus, Loki, Vector, status exporter, Loki gateway |
| `clusters/homelab/broker/` | Credential-injecting broker for agent traffic (`docs/broker.md`) |
| `apps/broker/` | Broker source and unit tests (Python stdlib, no dependencies) |
| `images/nanobot/` | Agent image source (Dockerfile, entrypoint, `render.py`); published by `publish-nanobot.yml` |
| `tests/policies/` | Static + runtime policy checks, fixtures, expectation tables |
| `tests/tofu/` | Static assertions on the Cloudflare config (`tests/tofu/check.sh`) |
| `tests/backup/` | Offline argument/URL shape tests for the repository mirror (`tests/backup/check.sh`) |
| `tests/digest/` | Digest builder fixtures and rendered contract (`tests/digest/check.sh`) |
| `tests/access/` | Live Access default-deny probe (`tests/access/check.sh`, skips without a token) |
| `tofu/` | Cloudflare OpenTofu (provider v5), Access default-deny, CAA (`docs/cloudflare.md`) |
| `scripts/` | CI helpers, shellchecked |
| `docs/` | Flux, tenancy, observability, broker, runbook, negative tests, manual steps |

## Local checks

```sh
nix run nixpkgs#kustomize -- build clusters/homelab/platform
nix run nixpkgs#yamllint -- .
nix run nixpkgs#shellcheck -- scripts/*.sh
scripts/check-action-pins.sh
scripts/check-pr-secret-boundary.sh
scripts/check-codeowners.sh
tests/policies/check.sh                  # static policy, RBAC, network and broker checks
python3 -m unittest discover -s apps/broker/tests -t apps/broker   # broker unit tests
tests/tofu/check.sh                      # Cloudflare config shape, no credentials
tests/backup/check.sh                    # mirror script shape, no network
tests/access/check.sh                    # live Access deny, skips without a token
tests/observability/check.sh             # vector unit tests, config lint, exporter tests
tests/digest/check.sh                    # digest builder fixtures and manifest contract
```

CI (`.github/workflows/ci.yml`) runs the same checks with pinned tool versions;
`.github/workflows/tofu-plan.yml` and `tofu-apply.yml` own the `cloudflare`
environment and run only on `main` or by manual dispatch.

## Ownership

Every path is owned by `@smores56`. `*.tf` and `.github/` are Kanbei-critical:
a change there can repoint `kanbei.sammohr.dev` or expose the Cloudflare token.
See `CODEOWNERS` and `docs/kanbei-critical.md`.
