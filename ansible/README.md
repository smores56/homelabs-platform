# Ubuntu host provisioning

Ansible for the Nix-free platform host: smortress (Ubuntu Server 26.04 LTS,
ext4 root, ZFS-on-LUKS data pool, NVIDIA, k3s agent) and tenant-vm (CPU-only
k3s server). Everything is plain YAML, idempotent, and safe to read. No secret
is stored in this tree; keyfiles, tokens, Restic passwords, and image digests
arrive from the operator or the private inventory.

## Layout

| Path | Purpose |
| --- | --- |
| `ansible.cfg` | Inventory default, privilege escalation, pipelining, no retry files |
| `inventory/example.yml` | Topology placeholders; copy to git-ignored `inventory/hosts.yml` |
| `group_vars/all.yml` | Documented knobs and safe defaults: LUKS, ZFS recovery groups, k3s pins, image keys |
| `playbooks/site.yml` | Full convergence, role by role |
| `playbooks/check.yml` | Inventory validation plus `site.yml` in check mode; refuses a mutating run |
| `playbooks/recover.yml` | Clean-room storage import and verified-generation restore outline |
| `playbooks/rescue.yml` | Applies independent rescue access, then refuses to pass until two SSH paths authenticate |
| `roles/base` | Packages, timezone, sysctl, users, SSH, unattended-upgrades, journald |
| `roles/luks` | LUKS2 keyfile slots and crypttab; disk initialisation only under `--tags destructive` |
| `roles/zfs` | Pool import/create, recovery-group datasets, mount ordering |
| `roles/storage` | `/srv` ownership, backup mounts, default-deny dataset inventory |
| `roles/k3s` | Pinned k3s, containerd handlers, RuntimeClass, kubeconfig mode |
| `roles/nvidia` | Driver and container-toolkit branch pins; device plugin owns no host config |
| `roles/kata` | Kata/QEMU packages and the `kata-qemu` RuntimeClass |
| `roles/data_plane` | Five containerised units, env files, hardening, timers |
| `roles/recovery` | Sentinel, portal constraints, clean-room entrypoint |
| `roles/rescue` | Independent sshd, optional break-glass account, Cloudflare rescue tunnel, optional Tailscale/WireGuard |

## Prerequisites

- Ubuntu Server installed by the signed autoinstall, SSH reachable as a sudo
  user. List only the LUKS volumes Ansible should own in `luks_volumes`; an
  autoinstall-owned root entry is preserved in `/etc/crypttab` because Ansible
  manages a marked block, not the whole file.
- Every LUKS container already carries a keyfile slot and an **offline
  passphrase slot** added once from Bitwarden. Ansible never creates, reads, or
  prints a passphrase.
- Keyfiles provisioned by the operator as `root:root`, mode `0400`, at the paths
  in `luks_volumes` (for example `/etc/luks/data.key`). The `failed` message
  names the missing path; it is not a secret.
- Every `data_plane_images` value is `repository@sha256:<64 hex>`, mirrored onto
  the host. Units run podman with `--pull=never`, so a digest cannot drift.
- On k3s agents, `/etc/rancher/k3s/node-token` is provisioned by the operator
  (path only in git; never the token value).
- `homelab_admin_users` contains at least one account with an SSH public key;
  otherwise the base role creates none. The provisioning account must already
  be in the `sudo` group because SSH hardening allows only `homelab_ssh_allow_groups`.
- Collections `community.general`, `community.crypto`, and `ansible.posix` are
  supplied by the `ansible` package used below.
- Rescue access is optional but fail-closed. When
  `rescue_breakglass_enabled`, `rescue_cloudflared_enabled`, or
  `rescue_vpn` is enabled, the operator must first place the corresponding
  root-owned file under `/etc/homelab/rescue/` (authorized keys, a rescue
  tunnel token distinct from the data-plane tunnel, or a Tailscale auth key /
  WireGuard config). A missing, empty, or group/world-readable file stops the
  run. No secret is stored in this tree or printed by a task.
- The optional VPN path is `rescue_vpn: none` by default. `tailscale` reads
  `rescue_tailscale_auth_key_file` (default
  `/etc/homelab/rescue/tailscale-auth.key`, mode `0400`); `wireguard` reads
  `rescue_wireguard_config_file` (default
  `/etc/wireguard/wg-rescue.conf`, mode `0600`). Both are operator-provisioned
  and fail closed when missing.
- `rescue_verify_paths` in `group_vars/all.yml` names at least two independent
  SSH endpoints; `playbooks/rescue.yml` opens a real session through each one.

## Operator sequence

Run the static check from the repository root, then work inside `ansible/`
so `ansible.cfg` (roles path, privilege escalation) is the one Ansible loads.

```sh
# 1. Static checks. No host contact, no network, missing tools skip.
nix-shell -p ansible yamllint --run 'ansible/tests/check.sh'

# 2. Enter the tree, copy the example inventory, and fill it in. The copy is
#    git-ignored; `-i inventory/hosts.yml` keeps site data out of the default.
cd ansible
cp inventory/example.yml inventory/hosts.yml
$EDITOR inventory/hosts.yml
```

```sh
# 3. First boot only: initialise the LUKS2 containers, format the empty backup
#    disks, and create the ZFS pool. Both the explicit tag and the confirmation
#    value are required; either one missing stops before a disk is touched.
#    (Review the inventory first; --check cannot review a pool that does not
#    exist yet.)
ansible-playbook -i inventory/hosts.yml playbooks/site.yml \
  --tags destructive -e homelab_destructive_confirm=true

# 4. Apply every non-destructive role: crypttab, volume open, pool import,
#    datasets, /srv layout, k3s, data-plane units.
ansible-playbook -i inventory/hosts.yml playbooks/site.yml

# 5. Drift gate. check.yml refuses to run without --check and then reviews the
#    full convergence, including inventory assertions.
ansible-playbook -i inventory/hosts.yml playbooks/check.yml --check --diff

# 6. Same drift report for a single host while iterating.
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --check --diff --limit smortress
```

No playbook performs a destructive disk operation without **both**
`--tags destructive` and `-e homelab_destructive_confirm=true`. `destructive`
covers only LUKS container creation and `zpool create`; formatting backup
filesystems uses the same gate and never runs untagged.

## Pre-migration rescue sequence

Do this before any migration phase that can affect SSH, storage, k3s, or the
data plane. The exact gate definitions and evidence are in
`docs/migration-gates.md`.

```sh
cd ansible

# 1. Declare both independent paths in group_vars/all.yml (or inventory/hosts.yml):
#    rescue_verify_paths:
#      - {name: lan, host: 192.0.2.10, port: 22, user: migration-breakglass}
#      - {name: tunnel, host: ssh-rescue.sammohr.dev, port: 22, user: migration-breakglass}

# 2. Install rescue access on every host. sshd is enabled and started first;
#    the optional paths are configured only when their operator file exists.
ansible-playbook -i inventory/hosts.yml playbooks/rescue.yml

# 3. Verify the same two paths by hand from the controller. Each must open a
#    session without a password prompt:
ssh -o BatchMode=yes -p 22 migration-breakglass@192.0.2.10 true
ssh -o BatchMode=yes -p 22 migration-breakglass@ssh-rescue.sammohr.dev true
```

`playbooks/rescue.yml` is itself the gate: it fails when fewer than two named
paths are declared, when any path cannot authenticate from the controller, or
when `ssh.service` is not running and enabled on the host. Keep the LAN path
and the independent rescue path working at all times; a cutover, reboot, or
970 conversion is not started while either one is unverified.

The Python preflight records the same facts and refuses the `protect_old` and
`cutover` gates without them:

```sh
cd ..
export PYTHONPATH="$PWD/libs"
python3 -m data_plane.migration preflight evidence.json
```

Both checks are required: Ansible proves the paths from the controller, and
the preflight records fresh, structured evidence that `protect_old` and
`cutover` can consume.

## What a host run produces

- `/etc/crypttab`: keyfile unlock for boot volumes; `noauto` for both backup
  disks. No `nofail` is ever written, so an unlock failure stops dependants.
- systemd drop-ins make `zfs-import-cache.service` require the encrypted data
  volume and `zfs-mount.service` require a successful import.
- Datasets: `tank/photos`, `tank/music`, `tank/books`, `tank/notes`,
  `tank/wallpapers`, `tank/memory/local`, `tank/memory/external`. The
  `tank/memory` parent is `canmount=off`; `compression=zstd`, `atime=off`, and
  per-group `recordsize` are enforced on every run.
- `/var/backups/primary` and `/var/backups/secondary` are fstab entries with
  `noauto,nodev,nosuid,noexec`. Backup units carry `RequiresMountsFor` and the
  matching `systemd-cryptsetup@…` requirements; a missing repository fails the
  unit instead of writing an unbacked snapshot.
- The default-deny inventory check reads `zfs list -r tank` and fails on any
  dataset not declared in `zfs_datasets`/`zfs_container_datasets`. Add a
  recovery group to `recovery_inventory` before creating a new dataset.
- `/etc/rancher/k3s/k3s.yaml` is `0600 root:root`; traefik, servicelb, and
  metrics-server are disabled; containerd gains runsc, nvidia, and kata-qemu
  handlers only on nodes that enable them.
- Data-plane units run with `NoNewPrivileges=yes`, `ProtectSystem=strict`, a
  read-only container root, all capabilities dropped, and `ReadWritePaths` for
  exactly the state and repository paths they own. `/srv` mounts are asserted
  with `AssertPathIsMountPoint`.

## Recovery

`recover.yml` assumes `site.yml` has already run on the clean host, so crypttab,
fstab, the zfs units, and the restore scripts exist. The generation manifest is
a local file outside git, copied to the host at the path passed to the playbook:

```json
{
  "complete": true,
  "destinations": [
    {
      "repository": "/var/backups/primary/personal",
      "password_file": "/etc/restic/personal.key",
      "snapshot": "<restic snapshot id>",
      "target": "/srv"
    }
  ]
}
```

```sh
# Dry run: opens storage, mounts backups, verifies the sentinel, prints the plan.
ansible-playbook -i inventory/hosts.yml playbooks/recover.yml \
  --tags destructive \
  -e homelab_recovery_confirm=true \
  -e homelab_recovery_generation_manifest=/root/generation.json

# Apply only after the plan is reviewed. `latest` is rejected by the entrypoint.
ansible-playbook -i inventory/hosts.yml playbooks/recover.yml \
  --tags destructive \
  -e homelab_recovery_confirm=true \
  -e homelab_recovery_apply=true \
  -e homelab_recovery_generation_manifest=/root/generation.json
```

Writers stay disabled after restore until the old writer is fenced or revoked,
a new epoch exists, old approvals are canceled, and anti-entropy completes.

## Pinned matrix

Bump these deliberately after re-testing the Ubuntu/kernel/OpenZFS/NVIDIA/k3s/
Kata matrix; none of them is a floating channel.

| Component | Pin |
| --- | --- |
| k3s | `k3s_version` (exact `vX.Y.Z+k3sN`) |
| gVisor | `gvisor_release` (release date) |
| Kata/QEMU | `kata_package_version` via apt preference |
| NVIDIA driver | `nvidia_driver_package` + `nvidia_driver_version` branch pin |
| NVIDIA toolkit | `nvidia_container_toolkit_version` branch pin |
| Data plane | `data_plane_images` digest per service |
