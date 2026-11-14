#!/usr/bin/env bash
# Static checks for the Ubuntu host provisioning tree.
#
#   nix-shell -p ansible yamllint --run 'ansible/tests/check.sh'
#
# No host is contacted and no network call is made. Missing tools are skipped
# with a warning rather than failing the run.
set -uo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root/ansible" || exit 1

pass=0
fail=0
skip=0

ok() {
  pass=$((pass + 1))
  printf 'ok   %s\n' "$1"
}

bad() {
  fail=$((fail + 1))
  printf 'FAIL %s\n' "$1"
  [[ -n ${2:-} ]] && printf '     %s\n' "${2//$'\n'/$'\n'     }"
  return 0
}

skipped() {
  skip=$((skip + 1))
  printf 'skip %s\n' "$1"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

if have ansible-playbook; then
  for playbook in playbooks/*.yml; do
    if output=$(ansible-playbook --syntax-check -i inventory/example.yml "$playbook" 2>&1); then
      ok "syntax $playbook"
    else
      bad "syntax $playbook" "$output"
    fi
  done
else
  skipped "ansible-playbook not found; syntax checks not run"
fi

if have yamllint; then
  if output=$(yamllint -c "$root/.yamllint.yaml" . 2>&1); then
    ok "yamllint"
  else
    bad "yamllint" "$output"
  fi
else
  skipped "yamllint not found; YAML lint not run"
fi

output=$(grep -rnE '^[[:space:]]*ignore_errors:' --include='*.yml' . 2>/dev/null || true)
if [[ -n $output ]]; then
  bad "no ignore_errors" "$output"
else
  ok "no ignore_errors"
fi

# The user module's `shell:` parameter and quoted templates are legitimate;
# only an unquoted/block shell module key is a violation.
output=$(grep -rnE '^[[:space:]]*(ansible\.builtin\.)?shell:' --include='*.yml' . 2>/dev/null \
  | grep -vE 'shell:[[:space:]]*["{]' || true)
if [[ -n $output ]]; then
  bad "no shell tasks" "$output"
else
  ok "no shell tasks"
fi

# The rescue role is the SSH invariant: it must exist with every optional
# path wired to an operator file, and it must fail closed when one is absent.
for required in \
  roles/rescue/defaults/main.yml \
  roles/rescue/tasks/main.yml \
  roles/rescue/handlers/main.yml \
  roles/rescue/meta/main.yml \
  roles/rescue/templates/rescue-sshd.conf.j2 \
  roles/rescue/templates/cloudflared-rescue.service.j2 \
  roles/rescue/templates/rescue-vpn.service.j2 \
  playbooks/rescue.yml; do
  if [[ -f $required ]]; then
    ok "rescue file $required"
  else
    bad "rescue file $required"
  fi
done

for default in \
  'rescue_breakglass_enabled: false' \
  'rescue_cloudflared_enabled: false' \
  'rescue_breakglass_sudo: false' \
  'rescue_vpn: none'; do
  if grep -qF "$default" roles/rescue/defaults/main.yml; then
    ok "rescue default $default"
  else
    bad "rescue default $default" "roles/rescue/defaults/main.yml must keep this off by default"
  fi
done

# Every optional rescue path must assert on its operator-supplied file.
for variable in \
  rescue_breakglass_authorized_keys_file \
  rescue_cloudflared_token_file \
  rescue_tailscale_auth_key_file \
  rescue_wireguard_config_file; do
  if grep -qF "$variable" roles/rescue/tasks/main.yml; then
    ok "rescue fail-closed $variable"
  else
    bad "rescue fail-closed $variable" "tasks/main.yml never references it"
  fi
done

# The rescue tunnel must not reuse the data-plane tunnel or its secret root.
if grep -qF 'rescue_data_plane_tunnel_token_file' roles/rescue/tasks/main.yml; then
  ok "rescue tunnel token is distinct"
else
  bad "rescue tunnel token is distinct" "tasks/main.yml has no distinct-token assertion"
fi

# Password authentication is never enabled, in any file or template.
output=$(grep -rniE '^[[:space:]]*PasswordAuthentication[[:space:]]+yes' \
  --include='*.yml' --include='*.j2' . 2>/dev/null || true)
if [[ -n $output ]]; then
  bad "no PasswordAuthentication yes" "$output"
else
  ok "no PasswordAuthentication yes"
fi

# Rescue units never require k3s, ZFS, or a data-plane unit.
output=$(grep -rnE '^(Requires|After|Wants)=.*(k3s|homelab-|data-plane|zfs)' \
  roles/rescue/templates 2>/dev/null || true)
if [[ -n $output ]]; then
  bad "rescue units independent of the data plane" "$output"
else
  ok "rescue units independent of the data plane"
fi

# The only file lookup is the public authorized-keys file; no key or token
# content is ever read into a task result or a debug message.
output=$(grep -rnE "lookup\(['\"]file['\"]" roles/rescue/tasks/main.yml 2>/dev/null \
  | grep -vF 'rescue_breakglass_authorized_keys_file' || true)
if [[ -n $output ]]; then
  bad "rescue reads no secret file contents" "$output"
else
  ok "rescue reads no secret file contents"
fi

output=$(grep -rnE 'var:[[:space:]]*rescue_.*(token|key)' \
  roles/rescue playbooks/rescue.yml 2>/dev/null || true)
if [[ -n $output ]]; then
  bad "rescue prints no secret values" "$output"
else
  ok "rescue prints no secret values"
fi

# The rescue playbook refuses to pass without two real SSH sessions.
if grep -qF 'rescue_verify_paths | length >= 2' playbooks/rescue.yml \
  && grep -qF 'BatchMode=yes' playbooks/rescue.yml; then
  ok "rescue verifies two SSH paths"
else
  bad "rescue verifies two SSH paths" "playbooks/rescue.yml must assert two paths and open a session through each"
fi

# Every download carries a checksum, and no unverified path remains.
downloads=$(grep -rn 'ansible\.builtin\.get_url:' --include='*.yml' roles/ | wc -l)
verified=$(grep -rn '^[[:space:]]*checksum:' --include='*.yml' roles/ | wc -l)
if [[ $downloads -gt 0 && $verified -ge $downloads ]]; then
  ok "every get_url in roles/ carries a checksum"
else
  bad "a get_url in roles/ has no checksum" "$downloads downloads, $verified checksums"
fi

output=$(grep -rn 'without a checksum\|sha256 | length == 0' --include='*.yml' roles/ 2>/dev/null || true)
if [[ -n $output ]]; then
  bad "no unverified download path" "$output"
else
  ok "no unverified download path"
fi

for needle in 'k3s_binary_sha256 is match' 'gvisor_sha256[item]'; do
  if grep -rqF "$needle" --include='*.yml' roles/ playbooks/; then
    ok "checksum guard present: $needle"
  else
    bad "checksum guard missing: $needle"
  fi
done

if grep -qF 'gvisor_sha256:' roles/k3s/defaults/main.yml; then
  ok "gVisor binaries require declared checksums"
else
  bad "gvisor_sha256 is not declared; an unverified download would be possible"
fi

# Kata is asserted through the runtime-rs shim and QEMU, not the Go CLI.
if grep -rqF '/usr/bin/kata-runtime' roles/ playbooks/ 2>/dev/null; then
  bad "the Go kata-runtime binary is referenced"
else
  ok "no reference to the Go kata-runtime binary"
fi
if grep -qF 'containerd-shim-kata-v2' roles/kata/defaults/main.yml; then
  ok "Kata asserts the runtime-rs shim"
else
  bad "Kata does not assert containerd-shim-kata-v2"
fi
if grep -qF 'ConfigPath' roles/k3s/templates/config.toml.tmpl.j2; then
  ok "the kata-qemu containerd handler names its QEMU configuration"
else
  bad "the kata-qemu handler has no ConfigPath"
fi

# Personal data is never world-readable.
if grep -qF 'storage_personal_mode: "0750"' roles/storage/defaults/main.yml &&
  grep -qF 'storage_public_mode: "0755"' roles/storage/defaults/main.yml; then
  ok "dataset modes default to personal 0750 and public 0755"
else
  bad "storage mode defaults do not distinguish personal from public"
fi
if grep -qF 'recovery_inventory[item.recovery_group].visibility' roles/storage/tasks/main.yml; then
  ok "served-tree mode is derived from recovery_inventory visibility"
else
  bad "served-tree mode is not derived from visibility"
fi
if grep -qF 'item.mode | default' roles/storage/tasks/main.yml; then
  bad "a per-dataset mode in group_vars can still widen a personal tree"
else
  ok "no per-dataset mode override is applied"
fi

# --------------------------------------------------------------------------
# Destructive gate
# --------------------------------------------------------------------------

# The single global boolean is retired: a leftover value must stop the run.
if grep -rqF 'homelab_destructive_confirm is not defined' playbooks/tasks/preflight.yml; then
  ok "a boolean destructive confirmation is rejected"
else
  bad "a boolean destructive confirmation is not rejected"
fi
output=$(grep -rn 'homelab_destructive_confirm' --include='*.yml' . 2>/dev/null \
  | grep -vF 'is not defined' || true)
if [[ -n $output ]]; then
  bad "the retired homelab_destructive_confirm is still referenced" "$output"
else
  ok "the retired destructive boolean is not referenced"
fi

# A per-host, per-device allow-list drives every destructive task.
for needle in \
  'homelab_destructive_device_ids' \
  "item.device in homelab_destructive_device_ids"; do
  if grep -rqF "$needle" roles/luks/tasks/main.yml roles/zfs/tasks/main.yml playbooks/tasks/preflight.yml; then
    ok "destructive allow-list: $needle"
  else
    bad "destructive allow-list missing: $needle"
  fi
done
if grep -qF "'destructive' in ansible_run_tags" roles/luks/tasks/main.yml &&
  grep -qF "'destructive' in ansible_run_tags" roles/zfs/tasks/main.yml; then
  ok "disk creation only runs under --tags destructive"
else
  bad "a disk task can run without --tags destructive"
fi

# The evidence file is re-run through the shared migration module.
for needle in \
  'homelab_migration_evidence' \
  'data_plane.migration' \
  'backup_freshness' \
  'restore_verification' \
  'rollback_disk_boot_tested' \
  'ssh_paths_independent'; do
  if grep -rqF "$needle" playbooks/tasks/migration-gate.yml; then
    ok "migration evidence gate: $needle"
  else
    bad "migration evidence gate missing: $needle"
  fi
done
if grep -qF 'tasks/migration-gate.yml' playbooks/site.yml; then
  ok "writers/backup steps require the migration gate"
else
  bad "site.yml never includes the migration gate"
fi

# LUKS refuses root, the 970, existing data, and demands a second keyslot +
# header backup before formatting; crypttab never duplicates autoinstall names.
for needle in \
  'luks_root_disks' \
  'homelab_rollback_disk_device' \
  'homelab_migration_rollback_released' \
  'allow_existing' \
  'keyslots' \
  '.header' \
  'luks_crypttab_managed' \
  "rejectattr('name', 'in'"; do
  if grep -qF "$needle" roles/luks/tasks/main.yml; then
    ok "luks safety: $needle"
  else
    bad "luks safety missing: $needle"
  fi
done

# ZFS imports are scoped to the pinned device, and both import units wait for
# systemd-cryptsetup.
if grep -qF 'argv: [zpool, import, -N, -d, "{{ zfs_pool_device }}", "{{ zfs_pool }}"]' roles/zfs/tasks/main.yml; then
  ok "zpool import is scoped to the pinned device"
else
  bad "zpool import is not scoped with -d"
fi
if grep -qF 'argv: [zpool, import, -N, "{{ zfs_pool }}"' roles/zfs/tasks/main.yml; then
  bad "an unscoped zpool import still exists"
else
  ok "no unscoped zpool import"
fi
if grep -qF 'zfs_import_scan_service' roles/zfs/tasks/main.yml &&
  grep -qF 'systemd-cryptsetup@{{ zfs_pool_luks_volume }}.service' roles/zfs/tasks/main.yml; then
  ok "both zfs import units require systemd-cryptsetup"
else
  bad "a zfs import unit can run before LUKS"
fi
if grep -qF 'zfs_pool_underlying_device in homelab_destructive_device_ids' roles/zfs/tasks/main.yml; then
  ok "the pool device must be allow-listed before import/create"
else
  bad "the pool import is not tied to the device allow-list"
fi

# Writers and the backup timer are not started by ordinary convergence.
if grep -qF 'writer: true' roles/data_plane/defaults/main.yml &&
  grep -qF 'not (item.writer | default(false))' roles/data_plane/tasks/main.yml &&
  grep -qF "item.short != 'backup-controller'" roles/data_plane/tasks/main.yml; then
  ok "write-broker and the backup timer stay stopped by default"
else
  bad "a writer or the backup timer starts by default"
fi
if grep -qF 'tags: [writers]' playbooks/site.yml &&
  grep -qF 'tags: [backup]' playbooks/site.yml; then
  ok "writers and backup are explicit tagged steps"
else
  bad "writers/backup are not explicit tagged steps"
fi

# --------------------------------------------------------------------------
# SSH lockout safety
# --------------------------------------------------------------------------

count=$(grep -cF 'validate: /usr/sbin/sshd -t -f %s' roles/base/tasks/main.yml)
if [[ ${count:-0} -ge 2 ]]; then
  ok "sshd configuration is validated before it is installed"
else
  bad "sshd validation is missing from roles/base"
fi
for needle in \
  'systemd-run' \
  '--on-active=' \
  'homelab_ssh_confirmation_marker' \
  'homelab_ssh_backup'; do
  if grep -qF -- "$needle" roles/base/tasks/main.yml; then
    ok "sshd revert safety: $needle"
  else
    bad "sshd revert safety missing: $needle"
  fi
done
if [[ -f roles/base/templates/ssh-revert.sh.j2 ]] &&
  grep -qF '/run/homelab-ssh-confirmed' README.md; then
  ok "the login-confirmation marker is installed and documented"
else
  bad "the SSH revert marker is not installed/documented"
fi
if grep -qF 'homelab_admin_group in' playbooks/tasks/preflight.yml &&
  grep -qF 'Require at least one administrator to hold a usable SSH key' roles/base/tasks/main.yml; then
  ok "admin-key and sudo membership checks exist"
else
  bad "the admin-key/sudo lockout check is missing"
fi
if grep -qF 'homelab_ssh_effective_allow_groups' roles/base/templates/99-hardening.conf.j2; then
  ok "AllowGroups is derived from the groups the admins hold"
else
  bad "AllowGroups is not derived from real admin groups"
fi

# --------------------------------------------------------------------------
# Restore safety
# --------------------------------------------------------------------------

for needle in \
  'manifest_hash' \
  'content hash' \
  '--allow-live' \
  'zfs receive' \
  'findmnt' \
  'writer marker'; do
  if grep -qF -- "$needle" roles/recovery/templates/clean-room-restore.sh.j2; then
    ok "restore safety: $needle"
  else
    bad "restore safety missing: $needle"
  fi
done
if grep -qF 'flags=(-u)' roles/recovery/templates/clean-room-restore.sh.j2 &&
  grep -qF 'flags=(-F -u)' roles/recovery/templates/clean-room-restore.sh.j2; then
  ok "zfs receive only uses -F behind the explicit allow-live flag"
else
  bad "zfs receive -F is not gated by --allow-live"
fi
if grep -qF 'restore does not require the portal' roles/recovery/templates/recovery-check.sh.j2; then
  ok "a stopped portal only warns; it is not a restore prerequisite"
else
  bad "the recovery portal is still a hard prerequisite"
fi
if grep -qF 'homelab_recovery_scratch_target' playbooks/recover.yml &&
  grep -qF 'HOMELAB_RESTORE_ROOT' playbooks/recover.yml &&
  grep -qF 'Re-enable the writers only after a verified scratch restore' playbooks/recover.yml; then
  ok "recover fences writers, restores to scratch, then re-enables"
else
  bad "recover.yml does not restore to scratch before re-enabling writers"
fi

if grep -qF "console=" roles/rescue/templates/99-homelab-serial.cfg.j2 && \
  grep -qF "serial-getty@" roles/rescue/tasks/main.yml; then
  ok "rescue keeps a local serial console independent of the network"
else
  bad "rescue has no serial console path"
fi
if grep -qF "dropbear-initramfs" roles/rescue/tasks/main.yml && \
  grep -qF "rescue_initramfs_ssh: false" roles/rescue/defaults/main.yml; then
  ok "initramfs SSH is available and disabled until a key is supplied"
else
  bad "initramfs SSH is missing or not fail-closed"
fi
if grep -qF "linux-image-*" group_vars/all.yml && \
  grep -qF "zfsutils-linux" group_vars/all.yml; then
  ok "kernel and ZFS upgrades are pinned out of unattended upgrades"
else
  bad "kernel or ZFS upgrades can drift unattended"
fi
if grep -qF "tags: [always]" playbooks/site.yml && \
  grep -qF "rescue.yml" playbooks/site.yml; then
  ok "the rescue path is re-proven on every tagged run"
else
  bad "tagged runs can skip the rescue proof"
fi

printf '\n%d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[[ $fail -eq 0 ]]
