#!/usr/bin/env bash
# Realtime progress and ETA for the per-tree restic Jobs (SDLC #619).
#
#   sudo scripts/restic-progress.sh                 # every tree, redrawn in place
#   sudo scripts/restic-progress.sh --tree navidrome
#   scripts/restic-progress.sh --once               # one snapshot, for a log
#
# It follows each Job's log (`kubectl logs --follow`) and renders the `--json`
# status records restic emits while it uploads: `percent_done`, `bytes_done` /
# `total_bytes` from restic's own scan, `files_done` / `total_files` and
# `seconds_remaining`. That beats sampling `/proc/<pid>/io`, whose counters
# include page cache, repository metadata and every earlier attempt in the same
# pod. A tree that emits no record says so instead of guessing.
#
# A Job that is not uploading is reported from its own state — `Failed
# (DeadlineExceeded)` and the like — because a deadline kill deletes the pod and
# with it the log; silence would otherwise look like a slow upload.
set -uo pipefail

KC=${KC:-}
interval=${INTERVAL:-1}

usage() {
  cat >&2 <<'EOF'
usage: scripts/restic-progress.sh [--tree <name>]... [--once]

  --tree    tree (namespace) to follow; repeatable, default all four
  --once    render one snapshot and exit instead of redrawing

environment: KC (client command, default `cluster-kubectl` as root, else
             `sudo cluster-kubectl`), INTERVAL (redraw seconds, default 1)
EOF
}

trees=()
once=0
while (($#)); do
  case $1 in
    --tree) trees+=("${2-}"); shift 2 || true ;;
    --once) once=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage; exit 64 ;;
  esac
done
((${#trees[@]})) || trees=(immich navidrome opdshelf graphiti)

if [[ -z $KC ]]; then
  if ((EUID == 0)); then KC=cluster-kubectl; else KC="sudo cluster-kubectl"; fi
fi
read -r -a kc <<<"$KC"
command -v "${kc[0]}" >/dev/null || { echo "$KC: not found" >&2; exit 2; }
command -v jq >/dev/null || { echo "restic-progress needs jq" >&2; exit 2; }

state=$(mktemp -d) || exit 2
pids=()
cleaned=0
# Each follower is its own process group (`set -m` below), so one kill takes the
# `kubectl logs --follow` under it as well; leaving those alive keeps a pty open
# long after the watcher has exited.
cleanup() {
  local pid
  ((cleaned)) && return
  cleaned=1
  for pid in "${pids[@]}"; do
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  done
  for pid in "${pids[@]}"; do
    kill -KILL -- "-$pid" 2>/dev/null
  done
  rm -r "$state" 2>/dev/null
}
int_exit() { cleanup; exit 0; }
trap int_exit INT TERM
trap cleanup EXIT

fmt_bytes() {
  numfmt --to=iec-i --suffix=B --format='%.1f' "$1" 2>/dev/null || printf '%sB' "$1"
}

fmt_span() {
  local s=$1 h m
  ((s < 0)) && s=0
  h=$((s / 3600)); m=$((s % 3600 / 60))
  if ((h > 0)); then printf '%dh%02dm' "$h" "$m"; else printf '%dm' "$m"; fi
}

bar() {
  local pct=$1 width=24 filled
  ((pct < 0)) && pct=0
  ((pct > 100)) && pct=100
  filled=$((pct * width / 100))
  printf '%s%s' "$(printf '%*s' "$filled" '' | tr ' ' '#')" \
    "$(printf '%*s' "$((width - filled))" '' | tr ' ' '.')"
}

newest_pod() {
  $KC -n "$1" get pods --field-selector=status.phase=Running \
    -l app.kubernetes.io/name=restic --sort-by=.metadata.creationTimestamp \
    -o name 2>/dev/null | tail -1
}

pod_health() {
  $KC -n "$1" get pods --field-selector=status.phase=Running \
    -l app.kubernetes.io/name=restic --sort-by=.metadata.creationTimestamp \
    -o json 2>/dev/null |
    jq -r '[.items[]] | last // {} | [
      (.metadata.name // ""),
      ([.status.containerStatuses[]?.restartCount] | max // 0),
      ([.status.containerStatuses[]?.lastState.terminated.reason]
         | map(select(. != null and . != "")) | first // "")] | @tsv' 2>/dev/null
}

# Follows one tree's newest restic pod, writing its last status record to
# $state/<tree>.json. The pod is re-resolved whenever the stream ends, because a
# retry replaces the pod and the name changes with it.
follow() {
  local ns=$1 pod line
  while :; do
    pod=$(newest_pod "$ns")
    if [[ -z $pod ]]; then
      rm -f "$state/$ns.json"
      sleep 2
      continue
    fi
    $KC -n "$ns" logs --tail=5 --follow "${pod#pod/}" 2>/dev/null |
      grep --line-buffered -F '"message_type":"status"' |
      while IFS= read -r line; do
        printf '%s\n' "$line" >"$state/$ns.json.new" &&
          mv "$state/$ns.json.new" "$state/$ns.json"
      done
    sleep 2
  done
}

# One line per tree. Writes into the global `line` so the caller owns printing.
render_tree() {
  local ns=$1 json pct fdone ftotal bdone btotal eta rate now prev_ts prev_b
  local job cond pod note podname restarts lastreason
  json="$state/$ns.json"
  if [[ -r $json ]]; then
    IFS=$'\t' read -r pct bdone btotal fdone ftotal eta < <(
      jq -r '[(((.percent_done // 0) * 100) | floor), (.bytes_done // 0),
              (.total_bytes // 0), (.files_done // 0), (.total_files // 0),
              (.seconds_remaining // 0)] | @tsv' <"$json" 2>/dev/null
    )
    # restic's own ETA is authoritative, but it reads 0 while restic is still
    # scanning and again at the very end; the byte rate covers those stretches.
    now=$(date +%s)
    rate=-1
    if [[ -r $state/$ns.rate ]] && read -r prev_ts prev_b <"$state/$ns.rate"; then
      if ((now > prev_ts && bdone > prev_b)); then
        rate=$(((bdone - prev_b) / (now - prev_ts)))
      fi
    fi
    printf '%s %s\n' "$now" "$bdone" >"$state/$ns.rate"
    if ((eta <= 0 && rate > 0 && btotal > bdone)); then
      eta=$(((btotal - bdone) / rate))
    fi
    line=$(printf '%-10s %s %3d%%  %10s / %-10s %s files' \
      "$ns" "$(bar "$pct")" "$pct" \
      "$(fmt_bytes "$bdone")" "$(fmt_bytes "$btotal")" "$fdone/$ftotal")
    ((eta > 0)) && line+=$(printf '  eta %s' "$(fmt_span "$eta")")
    ((rate > 0)) && line+=$(printf '  %s/s' "$(fmt_bytes "$rate")")
    ((btotal > 0)) || line+='  scanning'
    return
  fi

  job=$($KC -n "$ns" get jobs --sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -1)
  pod=$(newest_pod "$ns")
  if [[ -n $pod ]]; then
    read -r podname restarts lastreason <<<"$(pod_health "$ns")"
    line=$(printf '%-10s running %s, no status record yet' "$ns" \
      "${podname:-${pod#pod/}}")
    ((restarts > 0)) && line+=$(printf '  restarts=%s' "$restarts")
    [[ -n $lastreason ]] && line+=$(printf '  last=%s' "$lastreason")
    return
  fi
  # A failed Job carries both `FailureTarget` and `Failed` with the same reason,
  # so the raw jsonpath printed it twice. `jq` also turns the reason into the
  # sentence an operator needs: `BackoffLimitExceeded` explains nothing.
  cond=""
  if [[ -n $job ]]; then
    cond=$($KC -n "$ns" get "$job" -o json 2>/dev/null |
      jq -r '[.status.conditions[]? | select(.status == "True") | .reason]
             | unique | join(",")' 2>/dev/null)
  fi
  case $cond in
    CompletionsReached | Completed) note="completed" ;;
    DeadlineExceeded) note="killed by its deadline" ;;
    BackoffLimitExceeded) note="retries exhausted" ;;
    "") note="" ;;
    *) note="$cond" ;;
  esac
  if [[ -n $job ]]; then
    line=$(printf '%-10s not uploading — %s%s' "$ns" "${job#job.batch/}" \
      "${note:+ ($note)}")
  else
    line=$(printf '%-10s no Job in this namespace' "$ns")
  fi
}

draw() {
  local ns
  printf '%s' "$1"
  for ns in "${trees[@]}"; do
    line=""
    render_tree "$ns"
    printf '\033[K%s\n' "$line"
  done
}

set -m # followers become process-group leaders; see cleanup
for ns in "${trees[@]}"; do
  follow "$ns" &
  pids+=("$!")
done
set +m

if ((once)); then
  sleep 3 # let each follower connect and read one record
  draw ""
  exit 0
fi

drawn=0
while :; do
  ((drawn)) && printf '\033[%dA' "$drawn"
  draw "restic progress  (Ctrl-C stops the watcher; the Job keeps running)"$'\n'
  drawn=$((${#trees[@]} + 1))
  sleep "$interval"
done
