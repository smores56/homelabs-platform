#!/usr/bin/env bash
# Render nanobot's runtime state from the mounted tree, refresh the SDLC tooling
# on the tools PVC, then exec the pod's args unchanged (the pod passes
# `gateway --foreground --port 8080`). Nothing is cached across starts: the
# mounts are the only source of behaviour.
set -euo pipefail

: "${HOME:=/data/home}"
export HOME

# Everything the agent installs lives on the tools PVC, so it survives a restart
# and never needs a writable image layer. The defaults match the tenant
# Deployment (docs/flux.md, "SDLC tooling on the tools PVC"); an explicit value
# from the pod wins.
: "${TMPDIR:=$HOME/tmp}"
export TMPDIR
: "${UV_TOOL_DIR:=$HOME/tools/uv}"
export UV_TOOL_DIR
: "${UV_TOOL_BIN_DIR:=$HOME/tools/bin}"
export UV_TOOL_BIN_DIR
# Skills shell out to what the agent installed here, so the PVC must win over
# the image's own binaries.
export PATH="$UV_TOOL_BIN_DIR:$PATH"

python /usr/local/lib/nanobot/render.py

# The SDLC CLI and its skills are deliberately not baked into the image
# (SDLC #626): they are cloned through the broker's `github-sdlc-git` host,
# which injects the git basic auth for github.com, so the pod holds no
# credential, and installed into the tools PVC on every start. Every step is
# best-effort on purpose: a broker outage must start the agent without SDLC,
# not crashloop it. The previously installed tree, CLI and rendered ConfigMap
# skills stay as they were until the next restart retries.
bootstrap_extensions() {
  mkdir -p "$HOME/tools" "$UV_TOOL_BIN_DIR" "$TMPDIR" ||
    echo "nanobot-bootstrap: tool directories failed; continuing" >&2

  local src="$HOME/tools/sdlc-src"
  local next="$HOME/tools/sdlc-src.new"
  local previous="$HOME/tools/sdlc-src.previous"
  local skills_dst="$HOME/.nanobot/workspace/skills"
  local rev=none tool_count=0 skill_count=0 entry

  rm -rf "$next"
  if git clone --depth 1 --filter=blob:none \
      "${BROKER_URL%/}/github-sdlc-git/smores56/sdlc.git" "$next"; then
    # Swap only after a complete clone, so a failed fetch keeps the previous
    # tree instead of leaving a half-written one.
    rm -rf "$previous"
    if [ -d "$src" ]; then mv "$src" "$previous"; fi
    mv "$next" "$src"
    rm -rf "$previous"
  else
    echo "nanobot-bootstrap: sdlc clone failed; continuing" >&2
    rm -rf "$next"
  fi

  # The clone's revision is what the PVC now holds, which after a failed fetch
  # is the previously installed tree.
  if [ -d "$src/.git" ]; then
    rev=$(git -C "$src" rev-parse --short HEAD 2>/dev/null) || rev=none
  fi

  if [ -d "$src" ]; then
    uv tool install --force "$src" ||
      echo "nanobot-bootstrap: sdlc install failed; continuing" >&2
  fi

  # Overlay only: the render step owns the ConfigMap skills and a repository
  # without `skills/` must not empty them.
  if [ -d "$src/skills" ]; then
    mkdir -p "$skills_dst"
    cp -R "$src/skills/." "$skills_dst/" ||
      echo "nanobot-bootstrap: skills overlay failed; continuing" >&2
  fi

  for entry in "$UV_TOOL_BIN_DIR"/*; do
    if [ -e "$entry" ]; then tool_count=$((tool_count + 1)); fi
  done
  for entry in "$skills_dst"/*/SKILL.md; do
    if [ -e "$entry" ]; then skill_count=$((skill_count + 1)); fi
  done

  echo "nanobot-bootstrap: sdlc=$rev tools=$tool_count skills=$skill_count"
}

if [ -n "${BROKER_URL:-}" ]; then
  bootstrap_extensions
fi

exec nanobot "$@"
