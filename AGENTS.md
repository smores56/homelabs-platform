# Agent rules for homelabs-platform

## Hard rules

- This repository is human-merge only. Agents (nanobot) push to
  `smores56/homelabs-apps`; platform changes arrive as pull requests from a
  fork, which never receive the `cloudflare` environment or any secret.
- No secrets in git, in manifests, or in workflow files. Cluster secrets are
  SOPS+age ciphertext or ESO-generated; CI references none.
- Every GitHub Action reference is pinned to a 40-character commit SHA. CI fails
  otherwise (`scripts/check-action-pins.sh`).
- A workflow triggered by `pull_request` must not reference `secrets.*` other
  than `secrets.GITHUB_TOKEN`. `tofu-plan-pr.yml` is the single, checked
  exception: read-only plan credentials only, with the four guard markers
  `scripts/check-pr-secret-boundary.sh` requires. Never use
  `pull_request_target` (blocked by default in public repos from 2026-12-02).
- `*.tf` and `.github/` are Kanbei-critical (`docs/kanbei-critical.md`). Keep
  `CODEOWNERS`, that document, and `scripts/check-codeowners.sh` in sync.
- Anything an operator must do by hand goes in `docs/manual-steps.md` with the
  exact commands; no TODO/TBD placeholders.

## Before you commit

```sh
scripts/kustomize-build-all.sh          # every clusters/** kustomization renders
tests/policies/check.sh                 # static policy, RBAC, network, broker assertions
tests/policies/check.sh --runtime       # real admission + can-i probes on a throwaway
                                        # apiserver (CI job `policy admission runtime`)
python3 -m unittest discover -s apps/broker/tests -t apps/broker   # broker unit tests
tests/agent-control/check.sh            # agent-control unit tests (routes, scope, audit)
python3 -m unittest discover -s apps/webhook/tests -t apps/webhook  # webhook unit tests
tests/tofu/check.sh                     # Cloudflare config shape, no credentials
tests/backup/check.sh                   # mirror script args/URLs, no network
tests/observability/check.sh            # vector unit tests, config drift, exporter tests
tests/digest/check.sh                   # digest builder fixtures, egress and reverse-path contract
tests/data-plane/check.sh               # data-plane contracts, identity, journal, broker, backup, collectors, memory, CLI
nix run nixpkgs#yamllint -- .
nix run nixpkgs#actionlint -- .              # a workflow GitHub cannot parse never runs, silently
nix run nixpkgs#shellcheck -- scripts/*.sh tests/policies/*.sh tests/tofu/*.sh \
  tests/access/*.sh tests/backup/*.sh tests/digest/*.sh tests/observability/*.sh \
  tests/agent-control/*.sh clusters/homelab/flux-system/bootstrap.sh \
  clusters/homelab/platform/backup/*.sh
scripts/check-action-pins.sh
scripts/check-pr-secret-boundary.sh
scripts/check-codeowners.sh
git diff --check
```

Run `nix fmt` equivalents only where the repository defines them; this repo has
no formatter beyond `tofu fmt` (`nix run nixpkgs#opentofu -- fmt -check -recursive tofu`).

## Cluster conventions

- Flux runs with tenancy lockdown: per-controller flags are tabulated in
  `docs/flux.md` and asserted against the rendered args by
  `tests/policies/check.sh` (helm, notification, kustomize, source and
  source-watcher differ in which flags exist at the pinned versions). Per-tenant
  `decryption.secretRef` for age keys; the `--sops-age-secret` global stays
  unset. Never the two insecure kubeconfig flags.
- Namespaces carry `homelabs.dev/tenant` and `homelabs.dev/tier` labels plus PSA
  labels; non-platform namespaces enforce `restricted`. See `docs/tenancy.md`.
- Kustomization directories are self-contained; CI builds each one directly.
- Provider pin: Cloudflare v5 (`cloudflare_zero_trust_access_application`,
  `_policy`, `_network_hostname_route`, `cloudflare_dns_record`).
