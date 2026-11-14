# #660 T0 — multi-machine inventory

Status: **partial, root-gated**. Read-only survey of smorespro and smortress as
user `smores`, plus GitHub and the four implementation repositories. Items marked
`BLOCKED` need a credential or root read; each has the exact fill command in
[Blocked matrix](#blocked-matrix). No secret values were read or copied.

- Task: `smores56/sdlc#674`, feature #660
- Date: 2026-11-14
- Method: three read-only lanes (smorespro home, smortress user-readable state,
  GitHub/repo artifacts); filesystem and `gh` reads only
- Repo tips at survey time: `nix-config 42fbd956`, `homelabs-platform affe42d`,
  `homelabs-apps f987665`, `dotfiles 8f09ddd`

## 1. Machines and control planes

| Machine | Current authority / role | Access | Inventory state |
| --- | --- | --- | --- |
| smortress | NixOS bare metal (980 PRO) + tenant/Kanbei/cluster/ingest MicroVMs; host and cluster data apps; restic to HDD + R2 | `ssh smortress` as `smores` works; no root | User-readable paths surveyed; 12 data trees, `/var/backup/*`, and 4 MicroVM states are root-gated |
| smorespro | NixOS 26.11 (Zokor); personal client | Full user access (this host) | Complete for user data; `vault` Syncthing folder is missing on disk |
| smoreswork | macOS; work checkouts and work Graphiti | No SSH alias or local access | `BLOCKED` |
| phone | Syncthing peer; SMS/calls/contacts/Health Connect/MMS exports | Device reachable only through Syncthing records | Export locations `UNKNOWN` |
| Cloudflare | DNS, Access, tunnels, Pages, R2, OpenTofu state | No API token in session | `BLOCKED` |
| GitHub | 79 owned repos (61 archived, 11 private), GHCR images | `gh` works; token lacks `read:packages` | Repos surveyed; GHCR packages `BLOCKED` |
| Bitwarden | Account recovery kit, LUKS recovery passphrase, secret values | CLI absent and vault locked | `BLOCKED` |

## 2. Disks and mounts

| Device | Size | Fs | Mount | Use | Notes |
| --- | --- | --- | --- | --- | --- |
| `sda` (ST5000LM000) → `mapper/backup` | 4.5T | LUKS→ext4 | `/var/backup` | 5 TB HDD restic destination | 477G used, 3.9T free, 11% |
| `nvme0n1p2` → `mapper/root` | 930.5G | LUKS→ext4 | `/` | root, `/home`, `/var/lib/data` all here | **725G used, 144G free, 84%** |
| `nvme1n1p2` | 930.5G | LUKS | unmapped | likely the Samsung 970 rollback disk | Contents `BLOCKED`; do not touch |
| `loop0` | 256M | ext4 | `/var/lib/data/todo` | todo image | 230M fs, 275K used |
| `zram0` | 7.8G | swap | `[SWAP]` | — | active |

- `/srv` exists and is empty (`root:root 755`); the target `/srv/*` layout does
  not exist yet. No ZFS anywhere in the four repos — target-only.
- **Headroom risk:** `immich` (145G) + `music` (78G) alone exceed the 144G free
  on `/`. T6 sizing and the 970/HDD destinations need explicit math.
- Host LUKS/Clevis facts: `nix-config/modules/hosts/smortress/default.nix:138`
  (backup disk by-id), `disko.nix:31` (root LUKS), `boot.nix:25,45`
  (Clevis/Tang); `smorespro` TPM2 PIN + recovery passphrase
  (`modules/hosts/smorespro/boot.nix:16`, `disko.nix:29`).

## 3. Data sources

Sizes are `du -sx`; churn is files modified in the last 30 days. "Authority"
is the current live writer, not the design target.

| Source | Current authority | Location | Size | Files | 30d churn | Read | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Photos (live) | Immich | `/var/lib/data/immich` | 145G | 116,475 | 1,966 | yes | Live authority |
| Photos (legacy?) | unknown | `/var/lib/data/photos` (999:999, 0700) | `BLOCKED` | `BLOCKED` | `BLOCKED` | no | **Authority conflict**: resolve Immich vs legacy before migration |
| Music files | — | `/var/lib/data/music` | 78G | 12,077 | 7 | yes | Portable audio |
| Navidrome state | Navidrome | `/var/lib/data/navidrome` (987:984, 0700) | `BLOCKED` | `BLOCKED` | `BLOCKED` | no | Playlists/play counts |
| Books | files + opdshelf | `/var/lib/data/books` | 363M | 986 | 99 | yes | Setgid `smores:users`; `.opdshelf` metadata |
| Notes / vault | Syncthing | `/var/lib/data/vault` (vault, 0700) | `BLOCKED` | `BLOCKED` | `BLOCKED` | no | smorespro peer folder `~/Sync/vault` is **missing on disk** |
| Todos | Syncthing | `/var/lib/data/todo` (vault, 0700, loop) | 275K used | `BLOCKED` | `BLOCKED` | no | `todo.txt`/`done.txt` conversion target |
| Wallpapers | files | `/var/lib/data/wallpapers` | 2.4G | 383 | 120 | yes | — |
| Personal memory | Graphiti | `/var/lib/data/graphiti` | 1.6G | 22 | 14 | yes | db-dumps, falkordb, memory |
| Memory export | exporter | `/var/lib/data/memory-export` (root, 0700) | `BLOCKED` | `BLOCKED` | `BLOCKED` | no | Daily timer, 00:22 |
| Mail / finance / texts / browsing / calendar | collectors | `/var/lib/data/inbox` (root, 0750), `phone-untrusted`, `db` | `BLOCKED` | `BLOCKED` | `BLOCKED` | no | MicroVMs: `ingest-{finance,tuta,brave,immich}` |
| GitHub artifacts | mirror + PVC | `/var/lib/data/github-mirrors` (root, 0700); cluster PVC | `BLOCKED` | `BLOCKED` | `BLOCKED` | no | PVC can be lost with the tenant cluster |
| Infrastructure state | Tofu / cluster | `/var/backup/{CloudflareTofu,GitHub,RustFS}` (root, 0700) | `BLOCKED` | `BLOCKED` | `BLOCKED` | no | Human-only recovery class |

Additional readable trees: `open-webui`, `radicale`, `cluster-digest` are
retired or cross-tenant and are **not** migration sources.

## 4. Local Git state

Every checkout and worktree must be accounted for before ghq migration.

### smortress — `~/repos/github.com/smores56/`

| Repo | Branch | Dirty | Stash | Ahead/behind | Extra worktrees |
| --- | --- | --- | --- | --- | --- |
| `homelabs-apps` | main | 0 | 0 | 0/3 | — |
| `homelabs-platform` | main | **1 file** | 0 | 0/0 | — |
| `kanbei` | main | 0 | 0 | 0/1 | 5: `deterministic-append`, `implementation`, `quarantine-log`, `tuta-origin`, `single-process-workers` |
| `nix-config` | main | 0 | 0 | 0/0 | 2: `kanbei-streams`, `private-k3s-eval` (same tip `d8f6f546`, stale duplicate) |
| `sdlc` | main | 0 | 0 | 0/0 | — |

### smorespro — `~/repos/github.com/smores56/`

All four checkouts (`dotfiles`, `homelabs-platform`, `nix-config`,
`homelabs-apps`) are clean and in sync with `origin/main`.

- Orphan worktree pointer:
  `nix-config/.worktrees/smores-smoreswork-sops` points at a deleted
  `/home/smores/code/.../nix-config` gitdir and is absent from
  `git worktree list`. Safe to remove after confirming nothing valuable sits
  inside.
- `dotfiles` is **archived on GitHub** (last commit 2024-10-18). The design
  calls for a rebuilt repo; whether to unarchive or start fresh is a human
  decision.

## 5. Images

- Self-built GHCR: `nix-config/memory-mcp`, `homelabs-platform/nanobot`,
  `homelabs-platform/broker`, `homelabs-platform/webhook`.
  References: `homelabs-platform/clusters/homelab/data/graphiti/memory-mcp.yaml:71`,
  `.../broker/deployment.yaml:40`, `.../webhook/deployment.yaml:47`,
  `homelabs-apps/apps/nanobot/deployment.yaml:48`.
- Upstream pins worth preserving: `immich-server:v3.0.3`,
  `immich-machine-learning:v3.0.3`,
  `postgres:17-vectorchord0.4.3-pgvector0.8.1`, `navidrome:0.63.2`,
  `falkordb-server:v4.22.2`,
  `zepai/knowledge-graph-mcp:1.1.3-graphiti-0.31.2-standalone`,
  `restic:0.18.1`, `cloudflared:2026.10.2`, `ntfy:v2.37.2`.
- Full value/version pin list: `homelabs-platform/docs/data-migration.md` plus
  `clusters/homelab/**` and `homelabs-apps/apps/nanobot/**`.
- GHCR package existence/tags/visibility: `BLOCKED` (token lacks
  `read:packages`). No GitHub releases or Actions artifacts on any of the four
  repos.

## 6. Secrets and recovery dependencies

- SOPS files: `nix-config/secrets/{common,smorespro,smoreswork,smortress,cloudflare}.yaml`
  and `homelabs-platform/secrets/cloudflare.yaml`. Four host/client age
  recipients plus Cloudflare. Key→consumer map lives in
  `nix-config/docs/secrets.md` and `homelabs-platform/docs/secrets.md`.
- Decryption is **unavailable to this session**: the age key command
  (`SOPS_AGE_KEY_CMD`) exits 1 and no local age identity exists. No values were
  printed.
- Backup keys and restic passwords are held by Bitwarden; vault access is
  `BLOCKED`, so the recovery kit cannot be verified here.
- Host restic (`nix-config/modules/nixos/restic-backup.nix:44`): backs up
  `/var/lib/data` plus assistant/Letta state, excludes live SQLite/WAL, retains
  14 daily / 8 weekly / 12 monthly, runs 03:30, to `/var/backup/restic` and
  `s3:.../restic-backup` (`:105`).
- Per-app tree restic contracts exist for `immich`, `navidrome`, `opdshelf`,
  `graphiti` under `clusters/homelab/data/*/restic-*.yaml`; they dump first and
  preflight readability.
- GitHub backup: `clusters/homelab/platform/backup/cronjob.yaml` mirrors the two
  repos to the `github-backup-data` PVC. It is inside the tenant cluster and is
  not independently protected yet.
- Recovery site: `nix-config/recovery/` (Deno) + `tofu/pages.tf` project
  `sam-mohr-data-recovery`.
- Rollback path today: boot the old NixOS 980 and restore its latest HDD/R2
  generation; the 970 remains untouched until separate signoff.

## 7. Blocked matrix

| Gap | Why blocked | Fill with |
| --- | --- | --- |
| Sizes/counts/churn for `photos`, `vault`, `todo`, `phone-untrusted`, `navidrome`, `db`, `inbox`, `memory-export`, `github-mirrors`, `cluster-digest`, `radicale` | `0700`, other uid | `sudo scripts/inventory/collect-root.sh` (metadata only) |
| `/var/backup/*` repo contents and restic snapshot coverage | `0700 root` | Same script; `restic snapshots` needs the repo password from SOPS/Bitwarden |
| Restic success/schedule verification | `journalctl` for root units | Same script |
| k3s PVs/PVCs, images, CRs, GitHub-backup PVC | kubeconfig exists only inside the `cluster` MicroVM | Copy the read-only kubeconfig out of the guest |
| GHCR packages/tags/digests | token lacks `read:packages` | Token scope, or human check of the packages UI |
| R2 bucket/prefix sizes and object counts | no R2 credentials | Read-only R2 S3 credentials |
| Bitwarden items and recovery kit | CLI absent, vault locked | Names/types/locations-only export |
| smoreswork home and work checkouts | no access | SSH alias or run the collect script on the Mac |
| Phone exports | location unknown | Point at the SMS/calls/contacts/Health Connect/MMS export path |
| 970 contents | LUKS, unmapped | Passphrase + read-only mount, with explicit signoff |
| Immich vs legacy `photos` authority | both trees exist, one unreadable | Root read, then compare asset counts |

## 8. Decisions needed

1. Keep the inventory artifact here (`homelabs-platform/docs/inventory/`) or move
   it to the registry/issue.
2. `dotfiles`: unarchive and rebuild, or create a new repository.
3. Discard or keep: `~/.local/state/syncthing-versions` (37G), Steam/proton
   (26G), `~/.cache` (5.6G).
4. Classify `~/secrets-ingest` (217M, 726 files) and rotate/move the private key
   in `~/Downloads`.
5. Resolve the missing `~/Sync/vault` Syncthing folder before notes migration.
6. Confirm the 970 is the rollback disk and stays untouched until signoff.

## 9. T0 exit criteria

- Every source row is either measured or explicitly `BLOCKED` with an owner.
- Every checkout/worktree has a disposition (keep, migrate, delete).
- Immich/legacy photo authority is resolved.
- Restic destinations, schedules, and coverage are verified, not just configured.
- GHCR images, R2 object counts, and Bitwarden recovery items are enumerated.
- The machine reconciliation table in #660 has no `unknown` cells without a
  decision.
