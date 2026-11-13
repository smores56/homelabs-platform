# Secrets: who consumes a credential decides where it lives

Three stores exist, and a secret belongs to exactly one of them. The question is
never "which repo is convenient" but "what reads this value, and what may
decrypt it".

| Consumer | Store | Encrypted to | Applied by |
| --- | --- | --- | --- |
| Operator tooling in this repository (`tofu`, scripts) | `secrets/*.yaml` | the admin identity only | the operator, via `sops -d` |
| A workload this cluster runs | `clusters/**/*.sops.yaml` | the `flux-system` `sops-age` identity | Flux, with `spec.decryption` on the owning Kustomization |
| A host unit in `smores56/nix-config` | `nix-config/secrets/smortress.yaml` | the admin and host identities | sops-nix at boot |

The admin identity is the operator's passphrase-wrapped age key. Trusting it in
both repository stores is deliberate: everything here is maintained by one
person, and the alternative — a ciphertext file nobody can edit — is worse.

## This repository's operator store

`secrets/cloudflare.yaml` holds the Cloudflare credentials the tofu runs need:
`cloudflare-api-token` (the zone token) and the state-backend pair
`r2-state-access-key-id` / `r2-state-secret-access-key`. The state token is
scoped to `homelabs-tofu-state` alone and is deliberately **not** the host's
restic pair: state decides what the next apply does, so a backup credential must
not be able to rewrite it.

`scripts/tofu-cloudflare.sh` reads that file by default; `CLOUDFLARE_SECRETS_FILE`
overrides it, and pre-set `AWS_*` / `CLOUDFLARE_API_TOKEN` skip SOPS entirely
(that is how CI runs).

Create or edit it interactively, which keeps values out of shell history:

```sh
sops secrets/cloudflare.yaml
```

## Cluster ciphertext

Nothing is sealed this way yet; the mechanism is wired for the first workload
secret to move out of the operator's hands. The rules:

- the file lives beside the object it becomes and ends in `.sops.yaml`;
- the owning Kustomization carries the contract from
  `clusters/homelab/platform/secrets/sops-contract.yaml`, because the controller
  runs with `--no-cross-namespace-refs` and the `sops-age` Secret lives in
  `flux-system`;
- `.sops.yaml` gains the `flux-system` recipient when the first file lands, so a
  mistyped path fails to encrypt instead of landing in the wrong key group.

## What stays in `nix-config`, and what retires it

A credential whose daily reader is a host unit stays host-side until that unit is
gone. These are the holdovers, each with the task that ends it:

| nix-config key | Host reader | Retired by |
| --- | --- | --- |
| `cloudflare-api-token` | `alerting.nix` mail sender, `kanbei-access.nix` DNS-01, the `cloudflare-*` flake apps | alerting: #620 (mailrelay); flake apps: the step-14 state move; Kanbei: a DNS-scoped token of its own |
| `cloudflared-credentials` | the host tunnel (`web-proxy.nix`) | #615, when the last host-owned route moves |
| `restic-password`, `r2-access-key-id`, `r2-secret-access-key` | `restic-backup.nix` | #619's per-tree drill gate, one tree at a time |
| `memory-mcp-token` | the maki memory shim | #617's cluster route with an Access service token |

Until each row moves, the same value may exist in two stores. The host copy stays
authoritative for the host reader; the platform copy is written with the host
reader in mind, not copied blindly.

## Not secrets

Internal addresses, hostnames, account tags and recipients are configuration and
live in `tofu/terraform.tfvars`, `docs/`, or the manifests. Do not seal a value
that a manifest already publishes in the clear.
