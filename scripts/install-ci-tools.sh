#!/usr/bin/env bash
# Installs the pinned CI binaries. Versions and SHA-256 digests are explicit so a
# retagged or replaced upstream asset fails the checksum instead of running.
set -euo pipefail

dest="${1:-$HOME/.local/bin}"
mkdir -p "$dest"

KUSTOMIZE_VERSION=v5.8.3
KUSTOMIZE_SHA256=cb9e31198d3f63b44848bd0afc4c8efc56e6cfef9c93a4a39a211940e6decd61
KUBECONFORM_VERSION=v0.8.0
KUBECONFORM_SHA256=9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883
TOFU_VERSION=1.13.9
TOFU_SHA256=bc44eb6b06446ba83bd4072114891987f590f66cca712737a7f1147c50c539ce
VECTOR_VERSION=0.61.0
VECTOR_SHA256=84a27c3ad6b772fdb05bb76ea01a31498bfc8f5e71f32cc78a0d96841f1aa827

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fetch() {
  local url="$1" file="$2" sha256="$3"
  curl -sSfL -o "$tmp/$file" "$url"
  echo "$sha256  $tmp/$file" | sha256sum -c - >/dev/null
}

fetch "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" \
  kustomize.tar.gz "$KUSTOMIZE_SHA256"
tar -xzf "$tmp/kustomize.tar.gz" -C "$tmp" kustomize
install -m 0755 "$tmp/kustomize" "$dest/kustomize"

fetch "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" \
  kubeconform.tar.gz "$KUBECONFORM_SHA256"
tar -xzf "$tmp/kubeconform.tar.gz" -C "$tmp" kubeconform
install -m 0755 "$tmp/kubeconform" "$dest/kubeconform"

fetch "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" \
  tofu.zip "$TOFU_SHA256"
unzip -q -o "$tmp/tofu.zip" -d "$tmp/tofu"
install -m 0755 "$tmp/tofu/tofu" "$dest/tofu"

fetch "https://github.com/vectordotdev/vector/releases/download/v${VECTOR_VERSION}/vector-${VECTOR_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
  vector.tar.gz "$VECTOR_SHA256"
# The archive stores members as ./vector-.../bin/vector, so extracting a named
# member fails; unpack the tree and install from it.
tar -xzf "$tmp/vector.tar.gz" -C "$tmp"
install -m 0755 "$tmp/vector-x86_64-unknown-linux-gnu/bin/vector" "$dest/vector"

"$dest/kustomize" version
"$dest/kubeconform" -v
"$dest/tofu" version
"$dest/vector" --version

YQ_VERSION=v4.54.3
YQ_SHA256=129534601871538efe0ba4c5e62a34fb1d5f469bcb975c4fddeb467d82974973
fetch "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_amd64" \
  yq "$YQ_SHA256"
install -m 0755 "$tmp/yq" "$dest/yq"

"$dest/yq" --version
