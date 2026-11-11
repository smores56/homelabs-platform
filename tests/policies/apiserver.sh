#!/usr/bin/env bash
# Ephemeral Kubernetes control plane for admission tests: etcd plus
# kube-apiserver, no kubelet and no container runtime. Everything binds to
# loopback on a port derived from the PID, so it runs unprivileged on a
# workstation or in a throwaway VM.
#
#   tests/policies/apiserver.sh start   # boots, prints the kubeconfig path
#   tests/policies/apiserver.sh stop    # kills both processes
#
# Required on PATH: etcd, kube-apiserver, openssl.
#   nix shell nixpkgs#etcd nixpkgs#kubernetes nixpkgs#openssl -c \
#     tests/policies/apiserver.sh start
set -euo pipefail

state_dir="${POLICY_TEST_STATE:-${TMPDIR:-/tmp}/homelabs-policy-apiserver}"
kubeconfig="${state_dir}/kubeconfig"
token="policy-test-token"
base_port=$((20000 + ($$ * 7) % 20000))
etcd_port="${base_port}"
# etcd peer traffic takes base+1, the API server base+2; all three are disjoint.
api_port=$((base_port + 2))

require() {
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || {
      echo "missing required tool: $tool" >&2
      exit 1
    }
  done
}

start() {
  require etcd kube-apiserver openssl curl
  rm -rf "${state_dir}"
  mkdir -p "${state_dir}/etcd" "${state_dir}/certs"

  openssl genrsa -out "${state_dir}/certs/sa.key" 2048 2>/dev/null
  openssl rsa -in "${state_dir}/certs/sa.key" -pubout -out "${state_dir}/certs/sa.pub" 2>/dev/null
  # system:masters bypasses RBAC, so the same token drives setup and --as
  # impersonation probes.
  printf '%s,admin,admin,system:masters\n' "${token}" >"${state_dir}/tokens.csv"

  etcd \
    --data-dir "${state_dir}/etcd" \
    --listen-client-urls "http://127.0.0.1:${etcd_port}" \
    --advertise-client-urls "http://127.0.0.1:${etcd_port}" \
    --listen-peer-urls "http://127.0.0.1:$((etcd_port + 1))" \
    --initial-advertise-peer-urls "http://127.0.0.1:$((etcd_port + 1))" \
    --initial-cluster "default=http://127.0.0.1:$((etcd_port + 1))" \
    >"${state_dir}/etcd.log" 2>&1 &
  echo $! >"${state_dir}/etcd.pid"

  for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:${etcd_port}/health" >/dev/null 2>&1 && break
    sleep 1
  done
  # A dead etcd makes the apiserver time out instead of failing, which hides the
  # real cause; refuse to start the API server against it.
  if ! curl -sf "http://127.0.0.1:${etcd_port}/health" >/dev/null 2>&1; then
    echo "etcd did not become ready; see ${state_dir}/etcd.log" >&2
    tail -25 "${state_dir}/etcd.log" >&2 || true
    stop
    exit 1
  fi

  # --allow-privileged keeps the bench from rejecting privileged pods itself:
  # the policy, not the apiserver flag, is the control under test.
  kube-apiserver \
    --etcd-servers="http://127.0.0.1:${etcd_port}" \
    --secure-port="${api_port}" \
    --bind-address=127.0.0.1 \
    --advertise-address=127.0.0.1 \
    --token-auth-file="${state_dir}/tokens.csv" \
    --authorization-mode=RBAC \
    --service-account-issuer=https://kubernetes.default.svc \
    --service-account-key-file="${state_dir}/certs/sa.pub" \
    --service-account-signing-key-file="${state_dir}/certs/sa.key" \
    --service-cluster-ip-range=10.0.0.0/24 \
    --disable-admission-plugins=ServiceAccount \
    --allow-privileged=true \
    --cert-dir="${state_dir}/certs" \
    >"${state_dir}/apiserver.log" 2>&1 &
  echo $! >"${state_dir}/apiserver.pid"

  for _ in $(seq 1 "${POLICY_TEST_READY_SECONDS:-90}"); do
    if curl -skf "https://127.0.0.1:${api_port}/readyz" >/dev/null 2>&1; then
      break
    fi
    # Fail fast when the process is gone: the logs say why, and waiting out the
    # whole timeout hides it.
    if ! kill -0 "$(cat "${state_dir}/apiserver.pid")" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if ! curl -skf "https://127.0.0.1:${api_port}/readyz" >/dev/null 2>&1; then
    echo "apiserver did not become ready; see ${state_dir}/apiserver.log" >&2
    # CI cannot upload the state directory, so the reason has to be in the log.
    for log in apiserver etcd; do
      if [[ -f "${state_dir}/${log}.log" ]]; then
        echo "--- tail of ${log}.log" >&2
        tail -25 "${state_dir}/${log}.log" >&2
      fi
    done
    stop
    exit 1
  fi

  cat >"${kubeconfig}" <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
  - name: policy-test
    cluster:
      server: "https://127.0.0.1:${api_port}"
      insecure-skip-tls-verify: true
users:
  - name: policy-test
    user:
      token: "${token}"
contexts:
  - name: policy-test
    context:
      cluster: policy-test
      user: policy-test
current-context: policy-test
KUBECONFIG
  echo "${kubeconfig}"
}

stop() {
  for pidfile in "${state_dir}/apiserver.pid" "${state_dir}/etcd.pid"; do
    if [[ -f ${pidfile} ]]; then
      kill "$(cat "${pidfile}")" 2>/dev/null || true
      rm -f "${pidfile}"
    fi
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  *)
    echo "usage: $0 [start|stop]" >&2
    exit 2
    ;;
esac
