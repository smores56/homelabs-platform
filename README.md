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
| `clusters/homelab/flux-system/` | Flux bootstrap manifests (added by the bootstrap task) |
| `clusters/homelab/platform/` | Namespaces, tenant kustomizations, RBAC, policy, network |
| `clusters/homelab/data/` | Data-bearing apps: Immich, Navidrome, Calibre, Graphiti |
| `clusters/homelab/observability/` | Prometheus, Loki, Vector, status exporter |
| `clusters/homelab/broker/` | Egress broker for agent traffic |
| `tofu/` | Cloudflare OpenTofu (provider v5) |
| `scripts/` | CI helpers, shellchecked |
| `docs/` | Tenancy, runbook, negative tests, manual steps |

## Local checks

```sh
nix run nixpkgs#kustomize -- build clusters/homelab/platform
nix run nixpkgs#yamllint -- .
nix run nixpkgs#shellcheck -- scripts/*.sh
scripts/check-action-pins.sh
scripts/check-pr-secret-boundary.sh
scripts/check-codeowners.sh
```

CI (`.github/workflows/ci.yml`) runs the same checks with pinned tool versions;
`.github/workflows/tofu-plan.yml` and `tofu-apply.yml` own the `cloudflare`
environment and run only on `main` or by manual dispatch.

## Ownership

Every path is owned by `@smores56`. `*.tf` and `.github/` are Kanbei-critical:
a change there can repoint `kanbei.sammohr.dev` or expose the Cloudflare token.
See `CODEOWNERS` and `docs/kanbei-critical.md`.
