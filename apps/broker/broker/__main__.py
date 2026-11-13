"""Entrypoint: `python -m broker <enrollment.json>`.

Static secrets are files under `BROKER_SECRETS_DIR` (default `/run/secrets`, the
projected Secret mount); a missing file aborts startup. GitHub App enrollments
need only the app's private key at start — installation tokens are minted on
demand. A failure to mint is a 503 for that request, never a forwarded request
without its credential.
"""

from __future__ import annotations

import logging
import os
import sys

from .config import ConfigError, load_app_signing, load_config, load_secrets
from .credentials import AppCredentials, StaticCredentials
from .github_app import GithubAppTokens
from .server import main


def run(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m broker <enrollment.json>", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory = os.environ.get("BROKER_SECRETS_DIR", "/run/secrets")
    try:
        config = load_config(argv[0])
        secrets = load_secrets(config.hosts, directory)
        signing = load_app_signing(config, directory)
        if signing is not None:
            credentials = AppCredentials(
                secrets,
                GithubAppTokens(signing.app_id, signing.private_key),
                signing.installations,
            )
        else:
            credentials = StaticCredentials(secrets)
    except ConfigError as exc:
        print(f"broker: {exc}", file=sys.stderr)
        return 2
    return main(config, credentials)


if __name__ == "__main__":
    sys.exit(run())
