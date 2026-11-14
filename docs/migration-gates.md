# Migration gates and the rollback boundary

Source of truth: SDLC #660, "Migration and retirement gates" and "Ordered
migration". The gate implementation lives in
`libs/data_plane/migration/`; the rescue-access implementation lives in
`ansible/roles/rescue/` and `ansible/playbooks/rescue.yml`.

Two operator constraints outrank every other migration concern:

1. **SSH to smortress stays available** for the longest feasible window before
   and after the migration. Service downtime is acceptable; loss of SSH is not.
2. **No data may be lost.** A source is not retired, a route is not switched,
   and the 970 is not converted until a verified copy and a verified restore
   exist.

Everything below is offline: the Python CLI reads JSON evidence and prints a
plan or a failure list. It never contacts a host, opens a repository, or runs a
destructive command.

## Gate sequence

The phases are ordered and each one is exited only through its gate.

| # | Phase | Gate | Required evidence | Irreversible | Operator confirmation |
| --- | --- | --- | --- | --- | --- |
| 1 | `inventory` | `inventory-complete` | `machine_inventory`, `source_inventory`, `git_inventory` | no | no |
| 2 | `protect_old` | `old-system-protected` | `fresh_backup`, `restore_verified`, `rollback_disk_boot_tested`, `ssh_paths_independent` | no | no |
| 3 | `pilot` | `pilot-signed-off` | `pilot_client_signed_off` | no | no |
| 4 | `scratch_stack` | `scratch-stack-verified` | `scratch_stack_verified`, `recovery_spike_passed` | no | no |
| 5 | `shadow_backfill` | `shadow-reconciled` | `shadow_reconciled`, `fidelity_gaps_recorded` | no | no |
| 6 | `cutover` | `cutover-ready` | the four protection facts, plus `cutover_rehearsal_passed` | no (rollback window open) | **yes** |
| 7 | `retire_old` | `retirement-approved` | `retired_writers_stopped`, `new_generation_verified`, `rollback_no_longer_required` | **yes** | **yes** |

`protect_old` and `cutover` are the hard gates. The plan refuses to advance
past either without every listed fact; `cutover` additionally refuses without a
recorded human confirmation. `retire_old` is irreversible and therefore also
requires a confirmation by construction: the gate type rejects an irreversible
gate that does not require one.

A phase is not a script step. The operator performs the work, records evidence,
runs the preflight, and only then advances. The implementation is pure:
`advance(phase, evidence)` returns the next phase or raises `GateBlocked` with
every missing fact.

## Preflight evidence

`check_preflight` returns a structured report with one entry per failed
condition and never raises for missing fields, so every gap is visible at once.

```json
{
  "now": "2026-11-14T06:00:00+00:00",
  "required_destinations": ["primary", "secondary", "r2"],
  "required_groups": ["notes", "photos"],
  "backup_max_age_hours": 24,
  "restore_max_age_hours": 24,
  "ssh_max_age_hours": 24,
  "backups": [
    {
      "group": "notes",
      "destination": "primary",
      "created_at": "2026-11-14T05:30:00+00:00",
      "verified": true,
      "complete": true
    }
  ],
  "restore_verification": {
    "generation_id": "notes-20261114T0530",
    "storage": "scratch",
    "verified": true,
    "verified_at": "2026-11-14T05:45:00+00:00"
  },
  "rollback_disk": {
    "device": "/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_1TB",
    "present": true,
    "boot_tested": true,
    "converted": false,
    "released": false
  },
  "ssh_paths": [
    {
      "name": "lan",
      "kind": "direct",
      "verified": true,
      "independent_of_data_plane": true,
      "verified_at": "2026-11-14T05:55:00+00:00"
    },
    {
      "name": "tunnel",
      "kind": "cloudflared-rescue",
      "verified": true,
      "independent_of_data_plane": true,
      "verified_at": "2026-11-14T05:55:00+00:00"
    }
  ],
  "operator_confirmations": {
    "cutover": {"confirmed": true, "by": "smores", "at": "2026-11-14T05:58:00+00:00"},
    "retire_old": {"confirmed": true, "by": "smores", "at": "2026-11-14T05:58:00+00:00"}
  }
}
```

What each check proves:

| Check | Passes only when |
| --- | --- |
| `backup_freshness` | Every required group has a fresh (`backup_max_age_hours`), `verified: true`, `complete: true` capture on **every** required destination. A future-dated, stale, incomplete, or unverified capture does not count. |
| `restore_verification` | A generation was restored and verified on `storage: "scratch"` recently (`restore_max_age_hours`), and the generation id is named. A production restore is not proof. |
| `rollback_disk` | The 970 is `present`, `boot_tested`, named by an absolute device path, and neither `converted` nor `wiped`. `released: true` is the separate fact Ansible reads before it will let the 970 appear as a destructive target. |
| `ssh_paths` | At least two distinct paths are freshly verified, `independent_of_data_plane: true`, and not stale (`ssh_max_age_hours`). Two entries for the same route, or one path that rides the data plane, do not count. |
| `operator_confirmation` | Every phase with an irreversible or confirmation-required gate has `confirmed: true`, an operator (`by`), and a time (`at`). Unknown phases are rejected as typos. |

`fresh_backup`, `restore_verified`, `rollback_disk_boot_tested`, and
`ssh_paths_independent` are emitted as booleans in the report, and the
`operator_confirmations` mapping is carried through unchanged, so the same
report feeds `phases.advance` directly for `protect_old`, `cutover`, and
`retire_old`.

## Data-loss verification

`python -m data_plane.migration verify` compares a source snapshot and its
replica record by record. A pass requires all of:

- every source stable ID exists in the replica;
- the count matches exactly (no extra or missing IDs);
- every shared ID has the same lowercase SHA-256;
- every tombstone is audited and propagated;
- no tombstoned ID was resurrected in the replica;
- no ID was deleted only in the replica.

Missing IDs, count drift, hash mismatch, duplicate IDs, invalid hashes, and
unaudited tombstones all fail. `assert_no_data_loss` raises `DataLossError` with
the structured comparison attached. The comparison is deterministic: inputs are
sorted before reporting, so a repeated run produces the same failure list.

## Irreversible actions

These are the only actions in the migration that cannot be undone by booting
the old system:

| Action | Boundary | Requirement |
| --- | --- | --- |
| Converting the 970 from old-system rollback disk to a Restic destination | **Ends the old-system rollback path.** | `retire_old` gate, `rollback_no_longer_required`, explicit operator confirmation, and a verified new-system generation on every required destination. |
| Deleting retired application data and personal Graphiti state | Data is gone after retention expires | Approved `retire_old` confirmation plus the source-specific migration evidence (books/photos/music/notes/memory). Graphiti needs a restored new memory backup first. |
| Deleting or pruning old HDD/R2 generations | Old recovery points disappear | Reviewed retention process, not an ad-hoc `forget`/`prune`. |
| Reclassifying a source from personal to general | Prior external processing cannot be recalled | Confirmed bulk export and a new source epoch, per the #660 design. |

Everything else — route switches, writer pauses, re-enabling an old service —
stays reversible during the rollback window and must be reverted on failure.

## Rollback boundary

**Before the 970 is converted:**

1. Stop the new writers (`systemctl disable --now <units>`); SSH is not one of
   those units.
2. Make the old boot entry the default (`bootctl set-default <entry>`).
3. Restore the latest **verified** old generation through
   `/usr/local/sbin/homelab-clean-room-restore`, with the generation manifest
   and the restore confirmation.
4. Re-enable the independent SSH unit and re-verify **both** declared SSH
   paths before declaring rollback complete.

`rollback.py` renders exactly that order and is a dry run by default. A plan is
executable only with `dry_run=False`, and `apply_rollback` still needs the
literal confirmation `rollback`. The planner refuses a generation that is not
`verified: true`, and refuses outright once `rollback_disk.converted` or
`rollback_disk.wiped` is true.

**After the 970 is converted:** rollback as described above no longer exists.
Recovery depends solely on the new system's independently verified generations
(local NVMe, HDD, and R2, each captured from the same fenced snapshot). This
transition is a separate human approval, not a consequence of finishing
cutover.

The 970 is inventory-only during the migration. It is never formatted, and its
contents are not modified, before the `retire_old` gate passes. The 5 TB HDD is
never formatted during migration; new repositories are namespaced alongside the
old ones.

## SSH invariant

- `sshd` is enabled independently of k3s, ZFS, and every `homelab-*` data-plane
  unit. The rescue role starts and enables it before any optional path.
- The optional `migration-breakglass` account is SSH-public-key only, has a
  locked password, and receives no sudo unless
  `rescue_breakglass_sudo: true`. Its authorized-keys file is operator-supplied;
  a missing file fails the run.
- The optional `cloudflared-rescue.service` uses its own token file, distinct
  from the data-plane tunnel and outside the data-plane secret root. A missing
  or non-root-only token file fails the run. The token is passed to the service
  as a systemd credential, never in argv or the journal.
- The optional Tailscale or WireGuard path is `rescue_vpn: none` by default.
  Enabling it without its root-only auth-key/config file fails the run. The
  Tailscale unit uses `--auth-key=file:<path>` so the key never enters argv.
- `PasswordAuthentication yes` is never written; password authentication stays
  off from the base hardening drop-in.

## Ansible enforcement

The offline gates above are not advisory. Ansible re-runs the same preflight
module and refuses the corresponding host actions:

- A destructive run needs **all** of `--tags destructive`, a per-host
  `homelab_destructive_device_ids` allow-list of `/dev/disk/by-id/` paths, and
  `-e homelab_migration_evidence=<raw evidence.json>`. The Ansible gate invokes
  `python3 -m data_plane.migration preflight` and requires `fresh_backup`,
  `restore_verified`, `rollback_disk_boot_tested`, and
  `ssh_paths_independent`. Operator confirmations stay with the phase gates.
  The retired global boolean is rejected, not ignored.
- LUKS resolves every target to one canonical disk and refuses the root disk,
  the 970 unless the evidence says `rollback_disk.released: true`, and any
  device with an existing filesystem or pool unless the volume sets
  `allow_existing: true`. A second keyslot (offline passphrase) and a root-only
  header backup are required before any inner filesystem or pool is created.
  The managed `/etc/crypttab` block never repeats an autoinstall-owned name.
- ZFS imports only with `zpool import -N -d <pinned mapper>`, and only when the
  mapper's by-id device is allow-listed and the pool is absent. Both
  `zfs-import-cache.service` and `zfs-import-scan.service` require
  `systemd-cryptsetup@…`.
- A plain `site.yml` never starts `homelab-write-broker.service` or
  `homelab-backup-controller.timer`; `--tags writers` and `--tags backup` are
  separate, evidenced steps.
- `recover.yml` fences every writer first, restores the signed generation into a
  scratch target, verifies its content hashes, and only then re-enables the
  writers. A restore target that is a live mount or holds
  `.homelab-writer-active` is refused unless `--allow-live` is passed after
  fencing. `zfs receive` never uses `-F` on the default path. The recovery
  portal is not a prerequisite; a missing backup mount or a stopped portal is
  only a warning.

## Commands

Run from the repository root with `libs` on `PYTHONPATH` (the test runner does
this):

```sh
export PYTHONPATH="$PWD/libs"
python3 -m data_plane.migration preflight evidence.json
python3 -m data_plane.migration verify replica-evidence.json
python3 -m data_plane.migration rollback-plan rollback-evidence.json
```

Exit codes: `0` all gates hold; `1` a check failed (the JSON report names every
failure); `2` malformed evidence or usage. `--now` overrides the evidence clock
for a reproducible review.

There is no bypass flag. The Ansible tree forbids `ignore_errors`, and the
Python gate has no "force" or "skip" switch; changing the evidence file is the
only way to change a result, and that is reviewed.

## Exact pre-migration sequence

1. Complete the inventory and record it outside this repository.
2. Provision rescue access before touching production:

   ```sh
   cd ansible
   # Fill rescue_verify_paths in group_vars/all.yml or inventory/hosts.yml first.
   ansible-playbook -i inventory/hosts.yml playbooks/rescue.yml
   ```

   This installs/enables `sshd`, configures any optional path, and refuses to
   pass unless two independent paths authenticate from the controller.
3. Complete the old system's HDD/R2 backups, back up Cloudflare/OpenTofu state
   and GitHub artifacts, and populate the Bitwarden recovery kit.
4. Restore the latest old-system generation onto scratch storage and record its
   generation id. `storage` in the evidence must be `scratch`.
5. Boot the 970 rollback disk and record `boot_tested: true`. Do not modify it.
6. Write the preflight evidence, then run:

   ```sh
   python3 -m data_plane.migration preflight evidence.json
   ```

   Fix every reported failure. Re-run `playbooks/rescue.yml` and verify both
   SSH paths again immediately before any destructive step.
7. Only when `protect_old` evidence is complete may the pilot, scratch stack,
   shadow backfill, and cutover begin. Cutover still needs the confirmation.
8. `retire_old` (including the 970 conversion) is last and needs its own
   confirmation. Until then, the rollback plan above stays valid.
