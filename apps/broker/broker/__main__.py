"""Entrypoint: `python -m broker <enrollment.json>`.

Secrets are files under `BROKER_SECRETS_DIR` (default `/run/secrets`, the
projected Secret mount). A missing file aborts startup: the broker would
otherwise forward a request without its credential, which is a silent downgrade.
"""

from __future__ import annotations

import logging
import os
import sys

from .config import ConfigError, load_config, load_secrets
from .server import main


def run(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m broker <enrollment.json>", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(argv[0])
        secrets = load_secrets(config.hosts, os.environ.get("BROKER_SECRETS_DIR", "/run/secrets"))
    except ConfigError as exc:
        print(f"broker: {exc}", file=sys.stderr)
        return 2
    return main(config, secrets)


if __name__ == "__main__":
    sys.exit(run())
