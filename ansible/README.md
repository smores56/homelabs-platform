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
- `homelab_admin_users` contains at least one account, every account is in
  `sudo` (`homelab_admin_group`), and every account carries either an inline
  `ssh_authorized_keys` list or an `authorized_keys` path to a public-key file on
  the controller. The playbooks fail closed when no account has a usable key,
  and they derive `AllowGroups` from the groups the accounts actually hold.
  Never list a group in `homelab_ssh_allow_groups` that no admin belongs to.
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
# 3. First boot only: initialise the LUKS2 containers. A destructive run needs
#    all three of: --tags destructive, a per-host device allow-list, and a
#    reviewed migration evidence file. There is no global boolean.
#    (Review the inventory first; --check cannot review a pool that does not
#    exist yet.)
ansible-playbook -i inventory/hosts.yml playbooks/site.yml \
  --tags destructive \
  -e homelab_migration_evidence=/abs/path/evidence.json

# 4. Apply every non-destructive role: crypttab, volume open, pool import,
#    datasets, /srv layout, k3s, data-plane units. Writers and the backup timer
#    stay stopped.
ansible-playbook -i inventory/hosts.yml playbooks/site.yml

# 5. Drift gate. check.yml refuses to run without --check and then reviews the
#    full convergence, including inventory assertions.
ansible-playbook -i inventory/hosts.yml playbooks/check.yml --check --diff

# 6. Same drift report for a single host while iterating.
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --check --diff --limit smortress

# 7. Start the writers and the backup timer only after the evidence gate, one
#    explicit step at a time.
ansible-playbook -i inventory/hosts.yml playbooks/site.yml \
  --tags writers -e homelab_migration_evidence=/abs/path/evidence.json
ansible-playbook -i inventory/hosts.yml playbooks/site.yml \
  --tags backup -e homelab_migration_evidence=/abs/path/evidence.json
```

No playbook performs a destructive disk operation without **all** of
`--tags destructive`, a device in that host's `homelab_destructive_device_ids`,
and a passing migration evidence gate. The evidence file is the raw input to
`python3 -m data_plane.migration preflight`; Ansible re-runs the shared module
and requires fresh verified backups on every required destination, a scratch
restore, a boot-tested non-wiped 970, and two independent verified SSH paths.
`destructive` covers LUKS container creation, inner-filesystem creation, and
`zpool create`; formatting backup filesystems uses the same gate and never runs
untagged.

### Destructive device allow-list

`homelab_destructive_device_ids` lives per host (inventory or `host_vars`) and
contains stable `/dev/disk/by-id/` paths. The by-id name carries the serial or
WWN; each `luks_volumes` entry additionally declares `serial` or `wwn` and the
expected `size_bytes`. The LUKS role resolves every target to one canonical
disk, refuses the running root disk, refuses the 970 rollback disk unless the
evidence marks `rollback_disk.released: true`, and refuses a device with an
existing filesystem or pool unless the entry sets `allow_existing: true`.
Before any inner filesystem or pool is created, the container must carry a
second keyslot (the offline passphrase) and a root-only header backup at
`<luks_keyfile_dir>/<name>.header`.

### SSH change safety window

Replacing the hardening drop-in starts a transient
`systemd-run --on-active=300` revert unit. After the change, log in over SSH and
run:

```sh
sudo touch /run/homelab-ssh-confirmed
```

If the marker is still absent when the timer fires, the pre-Ansible drop-in is
restored from `/etc/ssh/sshd_config.d/.99-homelab-hardening.conf.pre-ansible`
(or removed if it did not exist) and `ssh.service` is restarted. The marker
lives in `/run`, so every reboot starts a new confirmation window. The drop-in
is always validated with `/usr/sbin/sshd -t -f %s` before it replaces the live
file, and the prepended `Include` line is validated the same way.

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
  disks. The managed block never repeats a name the autoinstall already wrote
  (`luks-root` stays autoinstall-owned), and the role asserts the finished file
  has no duplicate names. No `nofail` is ever written, so an unlock failure
  stops dependants.
- systemd drop-ins make **both** `zfs-import-cache.service` and
  `zfs-import-scan.service` require the encrypted data volume, and
  `zfs-mount.service` require a successful import. Ansible imports the pool only
  with `zpool import -N -d <pinned mapper>`, and only when that device is in the
  host's allow-list.
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
fstab, the zfs units, and the restore scripts exist. It fences every writer
(and the backup timer) first, restores into a scratch target, verifies the
restored content hashes, and only then re-enables the writers. A dry run touches
nothing. The recovery portal is never a prerequisite.

The manifest is the JSON written by
`data_plane.backup.generations.write_manifest` (all manifest fields plus
`manifest_hash`) with one extra top-level `restore` list. `complete: true` alone
is not accepted: the entrypoint recomputes the content hash, requires the schema
and every required destination, and refuses a `latest` snapshot.

```json
{
  "version": 1,
  "generation_id": "notes-20261114T0530",
  "group_id": "notes",
  "created_at": 1800000000.0,
  "datasets": [["tank/notes", "tank/notes@notes-20261114T0530"]],
  "sqlite": [],
  "app_versions": [],
  "content_hashes": [["/srv/notes/index.db", "<64-hex sha256>"]],
  "dependencies": [],
  "destinations": [
    {
      "destination": "primary",
      "snapshot_id": "<restic snapshot id>",
      "created_at": 1800000000.0,
      "group_id": "notes",
      "dataset": "tank/notes",
      "parent": null,
      "verified": true
    }
  ],
  "complete": true,
  "schema_version": 1,
  "release": "",
  "manifest_hash": "<64-hex sha256>",
  "restore": [
    {
      "kind": "restic",
      "repository": "/var/backups/primary/personal",
      "password_file": "/etc/restic/personal.key",
      "snapshot": "<manifest snapshot id>",
      "target": "/srv"
    }
  ]
}
```

```sh
# Dry run: opens storage, prints the plan. It does not fence writers.
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

The restore target must be a scratch directory: the entrypoint refuses a live
mount or a target holding `.homelab-writer-active` unless
`homelab_recovery_allow_live=true` is passed after fencing. `zfs receive` never
uses `-F` in the default path; the fenced override is the only way to enable
it. The sentinel check warns, but does not fail, on a missing backup mount or a
stopped portal.

Writers stay disabled after restore until the old writer is fenced or revoked,
a new epoch exists, old approvals are canceled, and anti-entropy completes. Set
`homelab_recovery_resume_writers=false` to leave them fenced after a verified
restore.

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
