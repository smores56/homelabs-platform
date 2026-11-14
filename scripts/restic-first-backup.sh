#!/usr/bin/env bash
# Start a first upload for one tree's restic repository (SDLC #619).
#
# `kubectl create job --from=cronjob/restic-backup` is not enough for this:
# `create job` takes no deadline flag, so the Job keeps the CronJob template's
# `activeDeadlineSeconds` and is killed part-way through the upload. That
# failure is `DeadlineExceeded`, which deletes the pod and with it the only copy
# of the log — it looks nothing like the upload it interrupted.
#
# So: create the Job, patch the deadline onto it, then read the deadline back
# from the API. The read-back is the part that matters, because an override that
# silently did not apply is indistinguishable from a healthy upload until the
# Job dies an hour later.
#
#   scripts/restic-first-backup.sh --tree navidrome
#   KC="sudo cluster-kubectl" scripts/restic-first-backup.sh --tree immich
#
# It refuses two states that cannot work: a tree that has already had a
# successful run (the nightly Job is the right path then), and restic already
# running in the namespace (the repository lock is not shared, and a second
# upload re-reads the tree from byte 0).
set -uo pipefail

KC=${KC:-kubectl}
DEADLINE=${DEADLINE:-86400}
BACKOFF_LIMIT=${BACKOFF_LIMIT:-6}

usage() {
  cat >&2 <<'EOF'
usage: scripts/restic-first-backup.sh --tree <name> [--deadline <seconds>]

  --tree      tree whose restic-backup CronJob is cloned; the namespace of the
              same name is where the Job runs
  --deadline  activeDeadlineSeconds applied to the Job (default 86400)

environment: KC (kubectl command, default `kubectl`), BACKOFF_LIMIT (default 6)
EOF
}

tree=""
while (($#)); do
  case $1 in
    --tree) tree=${2-}; shift 2 || true ;;
    --deadline) DEADLINE=${2-}; shift 2 || true ;;
    -h | --help) usage; exit 0 ;;
    *) usage; exit 64 ;;
  esac
done
[[ -n $tree ]] || { usage; exit 64; }
[[ $DEADLINE =~ ^[0-9]+$ ]] || { echo "--deadline takes whole seconds" >&2; exit 64; }

$KC -n "$tree" get cronjob restic-backup >/dev/null 2>&1 ||
  { echo "namespace $tree has no restic-backup CronJob" >&2; exit 2; }

if [[ -n $($KC -n "$tree" get cronjob restic-backup -o jsonpath='{.status.lastSuccessfulTime}') ]]; then
  echo "$tree has already had a successful run; let the nightly Job cover it" >&2
  exit 3
fi

if [[ -n $($KC -n "$tree" get pods -l app.kubernetes.io/name=restic \
  --field-selector=status.phase=Running -o name) ]]; then
  echo "restic is already running in $tree; one upload per repository" >&2
  exit 4
fi

name="first-$tree-$(date +%s)"
$KC -n "$tree" create job --from=cronjob/restic-backup "$name" >/dev/null
$KC -n "$tree" patch job "$name" --type=merge >/dev/null \
  -p "{\"spec\":{\"activeDeadlineSeconds\":$DEADLINE,\"backoffLimit\":$BACKOFF_LIMIT}}"

applied=$($KC -n "$tree" get job "$name" -o jsonpath='{.spec.activeDeadlineSeconds}')
if [[ $applied != "$DEADLINE" ]]; then
  echo "$name: activeDeadlineSeconds is ${applied:-missing}, not $DEADLINE" >&2
  echo "deleting it rather than letting it die mid-upload" >&2
  $KC -n "$tree" delete job "$name" >/dev/null
  exit 5
fi

printf '%s: first upload started in %s\n' "$name" "$tree"
printf '  deadline %ss, backoffLimit %s\n' "$applied" "$BACKOFF_LIMIT"
printf '  follow:  %s -n %s logs -f job/%s --all-containers\n' "$KC" "$tree" "$name"
printf '  stop:    %s -n %s delete job %s\n\n' "$KC" "$tree" "$name"
cat <<'EOF'
Do not run `restic forget --prune` against this repository before the Job has
produced its first snapshot: until then every uploaded pack is unreferenced and
a prune collects it. A killed attempt loses only the re-read, because restic
skips blobs the repository already holds.
EOF
