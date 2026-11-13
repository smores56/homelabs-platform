#!/usr/bin/env bash
# Render nanobot's runtime state from the mounted tree, then exec the pod's args
# unchanged (the pod passes `gateway --foreground --port 8080`). Nothing is
# cached across starts: the mounts are the only source of behaviour.
set -euo pipefail

: "${HOME:=/data/home}"
export HOME

python /usr/local/lib/nanobot/render.py

exec nanobot "$@"
