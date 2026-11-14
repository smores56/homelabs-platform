#!/usr/bin/env bash
# Data-plane unit tests. Static only: no cluster, no credentials, no network.
set -uo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root" || exit 1

export PYTHONPATH="$root/libs${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

exec python3 -m unittest discover -s tests/data-plane -t tests/data-plane -v
