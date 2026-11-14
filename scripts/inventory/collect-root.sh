#!/bin/sh
# Read-only root inventory for sdlc #674 (feature #660).
#
# Run as root on smortress:
#   sudo scripts/inventory/collect-root.sh [output-dir]
#
# Default output: /home/smores/wip/660-inventory/raw
#
# This script records metadata only. It does not read credential contents,
# restic passwords, SOPS values, or private keys, and it does not change state.
set -eu

OUT="${1:-/home/smores/wip/660-inventory/raw}"
umask 077
mkdir -p "$OUT"

echo "writing metadata to $OUT"

{
  date -u
  uname -a
  . /etc/os-release
  echo "os=${PRETTY_NAME:-unknown}"
} >"$OUT/identity.txt"

lsblk -J >"$OUT/lsblk.json"
findmnt -J >"$OUT/findmnt.json"
{ echo "--- fstab ---"; cat /etc/fstab; echo "--- crypttab ---"; cat /etc/crypttab; } \
  >"$OUT/fstab-crypttab.txt" 2>&1 || true

# uid -> name for the owners seen in blocked trees
getent passwd 71 983 984 985 987 988 999 >"$OUT/owners.txt" 2>&1 || true

# Per-tree size, file count, and churn. Metadata only.
: >"$OUT/data-trees.csv"
echo "path,bytes,files,mtime_7d,mtime_30d,mtime_90d" >>"$OUT/data-trees.csv"
for d in /var/lib/data/*; do
  [ -d "$d" ] || continue
  bytes=$(du -sx -B1 "$d" 2>/dev/null | awk '{print $1}')
  files=$(find "$d" -xdev -type f 2>/dev/null | wc -l)
  d7=$(find "$d" -xdev -type f -mtime -7 2>/dev/null | wc -l)
  d30=$(find "$d" -xdev -type f -mtime -30 2>/dev/null | wc -l)
  d90=$(find "$d" -xdev -type f -mtime -90 2>/dev/null | wc -l)
  echo "$d,$bytes,$files,$d7,$d30,$d90" >>"$OUT/data-trees.csv"
done

# Backup destination: sizes and names only.
: >"$OUT/backup-trees.csv"
echo "path,bytes" >>"$OUT/backup-trees.csv"
for d in /var/backup/*; do
  [ -e "$d" ] || continue
  bytes=$(du -sx -B1 "$d" 2>/dev/null | awk '{print $1}')
  echo "$d,$bytes" >>"$OUT/backup-trees.csv"
done
find /var/backup -maxdepth 3 -printf '%u:%g %m %s %p\n' 2>/dev/null \
  >"$OUT/backup-tree.txt" || true

# MicroVM state paths (no contents).
ls -la /var/lib/microvms >"$OUT/microvms.txt" 2>&1 || true
find /var/lib/microvms -maxdepth 5 \( -name 'kubeconfig*' -o -name 'k3s.yaml' \) \
  -printf '%m %u:%g %p\n' 2>/dev/null >"$OUT/kubeconfig-paths.txt" || true

# Backup/restic/cluster units and recent results.
{
  systemctl list-timers --all --no-pager
  echo
  systemctl list-units --type=service --all --no-pager
} >"$OUT/systemd.txt" 2>&1 || true

journalctl -u 'restic-backups-*' --since '7 days ago' --no-pager 2>/dev/null \
  | tail -n 400 >"$OUT/restic-journal.txt" || true

# Restic repositories: locations and on-disk size only.
# Snapshot listing is deliberately skipped: it needs the repo password, which
# must not be read by this script. Run `restic snapshots` manually with the
# password from Bitwarden/SOPS if coverage must be verified.
find /var/backup -maxdepth 4 -type d -name restic -printf '%p\n' 2>/dev/null \
  >"$OUT/restic-repos.txt" || true

# Syncthing folder markers tell us which trees are live writers.
find /var/lib/data -maxdepth 2 -name '.stfolder' -printf '%h\n' 2>/dev/null \
  >"$OUT/syncthing-folders.txt" || true

chmod -R go-rwx "$OUT"
echo "done: $OUT"
echo "no credential contents were read"
