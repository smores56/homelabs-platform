#!/bin/sh
# Mirror one GitHub repository into a bare mirror and a single-file bundle
# (SDLC #583). Runs as the `github-backup` CronJob in the platform namespace; the
# only credential is a fine-grained, read-only PAT mounted as a file.
#
# The token is never an argument, never in the URL and never printed: git asks
# for a username/password through GIT_ASKPASS, and the askpass helper reads the
# token from the environment of the git process. A token in the clone URL would
# be written into the mirror's remote config and the reflog.
#
#   github-backup.sh --owner smores56 --repo homelabs-platform --out /backup
#   github-backup.sh --owner smores56 --repo homelabs-platform --dry-run
#   github-backup.sh --owner smores56 --repo homelabs-platform --print-url
#
# --print-url and --dry-run are the offline shapes tests/backup/check.sh
# exercises; neither touches the network.
set -eu

owner=
repo=
out=${GITHUB_BACKUP_OUT:-/backup}
base=${GITHUB_BACKUP_BASE:-https://github.com}
token_file=${GITHUB_BACKUP_TOKEN_FILE:-/var/run/secrets/github-backup/token}
mode=run

usage() {
  cat >&2 <<'EOF'
usage: github-backup.sh --owner <owner> --repo <repo> [--out <dir>]
                        [--token-file <path>] [--base <url>]
                        [--dry-run | --print-url]

  --owner       GitHub user or organization (case-insensitive, no slash)
  --repo        repository name (no slash)
  --out         directory for mirror/ and bundles/ (default /backup)
  --token-file  file holding a fine-grained PAT (default
                /var/run/secrets/github-backup/token)
  --base        clone base URL (only https://github.com is accepted)
  --dry-run     validate everything, print the plan, no network
  --print-url   print the clone URL and exit (no token read)
EOF
  exit 2
}

die() {
  printf 'github-backup: %s\n' "$*" >&2
  exit 1
}

valid_owner() {
  case $1 in
    '' | *[!A-Za-z0-9-]*) return 1 ;;
    -* | *-) return 1 ;;
  esac
  [ "${#1}" -le 39 ]
}

valid_repo() {
  case $1 in
    '' | . | .. | *[!A-Za-z0-9._-]*) return 1 ;;
  esac
  [ "${#1}" -le 100 ]
}

valid_base() {
  # Only github.com: a GHES hostname would need its own egress allowlist and
  # token contract, so it is refused rather than silently attempted.
  [ "$1" = "https://github.com" ]
}

while [ $# -gt 0 ]; do
  case $1 in
    --owner)
      [ $# -ge 2 ] || usage
      owner=$2
      shift 2
      ;;
    --repo)
      [ $# -ge 2 ] || usage
      repo=$2
      shift 2
      ;;
    --out)
      [ $# -ge 2 ] || usage
      out=$2
      shift 2
      ;;
    --token-file)
      [ $# -ge 2 ] || usage
      token_file=$2
      shift 2
      ;;
    --base)
      [ $# -ge 2 ] || usage
      base=$2
      shift 2
      ;;
    --dry-run)
      mode=dry-run
      shift
      ;;
    --print-url)
      mode=print-url
      shift
      ;;
    -h | --help) usage ;;
    *) printf 'github-backup: unknown argument: %s\n' "$1" >&2 && usage ;;
  esac
done

[ -n "$owner" ] || die "missing --owner"
[ -n "$repo" ] || die "missing --repo"
valid_owner "$owner" || die "invalid owner: $owner"
valid_repo "$repo" || die "invalid repository: $repo"
valid_base "$base" || die "base must be exactly https://github.com"
case $out in
  /*) ;;
  *) die "output directory must be absolute: $out" ;;
esac

url=$base/$owner/$repo.git
mirror_root=$out/mirror
bundle_root=$out/bundles
mirror=$mirror_root/$owner/$repo.git
bundle=$bundle_root/$owner/$repo.bundle

if [ "$mode" = print-url ]; then
  printf '%s\n' "$url"
  exit 0
fi

[ -f "$token_file" ] || die "token file not found: $token_file"
token=$(head -n1 "$token_file" | tr -d '\r')
case $token in
  "") die "token file is empty: $token_file" ;;
  github_pat_*) ;;
  *) die "token is not a fine-grained PAT (expected a github_pat_... value); a classic ghp_ token is refused" ;;
esac
case $token in
  *[!A-Za-z0-9_]*) die "token contains unexpected characters" ;;
esac

if [ "$mode" = dry-run ]; then
  printf 'url=%s\n' "$url"
  printf 'mirror=%s\n' "$mirror"
  printf 'bundle=%s\n' "$bundle"
  printf 'token=fine-grained\n'
  exit 0
fi

umask 077
mkdir -p "$mirror_root/$owner" "$bundle_root/$owner"

askpass_dir=$(mktemp -d "${TMPDIR:-/tmp}/github-backup.XXXXXX")
work_mirror=$mirror_root/$owner/.$repo.git.new
cleanup() {
  rm -rf "$askpass_dir" "$work_mirror"
}
trap cleanup EXIT HUP INT TERM

cat >"$askpass_dir/askpass" <<'ASKPASS'
#!/bin/sh
case ${1:-} in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*) printf '%s\n' "${GITHUB_BACKUP_TOKEN:-}" ;;
  *) exit 1 ;;
esac
ASKPASS
chmod 0700 "$askpass_dir/askpass"

printf 'github-backup: mirroring %s/%s\n' "$owner" "$repo"
rm -rf "$work_mirror"
GIT_TERMINAL_PROMPT=0 \
  GIT_ASKPASS="$askpass_dir/askpass" \
  GITHUB_BACKUP_TOKEN="$token" \
  git clone --mirror --quiet "$url" "$work_mirror"
rm -rf "$mirror"
mv "$work_mirror" "$mirror"

commit=none
if git -C "$mirror" rev-parse --verify HEAD >/dev/null 2>&1; then
  commit=$(git -C "$mirror" rev-parse HEAD)
  git -C "$mirror" bundle create "$bundle.tmp" --all
  mv "$bundle.tmp" "$bundle"
else
  # An empty repository has no refs; git refuses to write an empty bundle.
  rm -f "$bundle"
fi

refs=$(git -C "$mirror" for-each-ref --format='%(refname)' | wc -l | tr -d ' ')
if [ -f "$bundle" ]; then
  bytes=$(wc -c <"$bundle" | tr -d ' ')
else
  bytes=0
fi

{
  printf 'repo=%s/%s\n' "$owner" "$repo"
  printf 'url=%s\n' "$url"
  printf 'commit=%s\n' "$commit"
  printf 'refs=%s\n' "$refs"
  printf 'bundle=%s\n' "$bundle"
  printf 'bundle_bytes=%s\n' "$bytes"
  printf 'updated=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$bundle.manifest.tmp"
mv "$bundle.manifest.tmp" "$bundle.manifest"

printf 'github-backup: %s/%s at %s (%s refs, %s bytes)\n' \
  "$owner" "$repo" "$commit" "$refs" "$bytes"
