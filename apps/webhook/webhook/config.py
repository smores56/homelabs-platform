"""Environment configuration; every credential is a mounted file.

The pod starts with no token if the Secret is missing, which is the fail-closed
behaviour the broker uses: a receiver that cannot enqueue is useless but safe,
while one that starts without its admin token would accept anonymous
registration on a public hostname.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = "/data"
DEFAULT_ADDRESS = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_GATEWAY_URL = "http://nanobot.tenant-apps.svc.cluster.local:8765"
DEFAULT_GATEWAY_TOKEN_FILE = "/run/secrets/enqueue-token"
DEFAULT_ADMIN_TOKEN_FILE = "/run/secrets/admin-token"
DEFAULT_PUBLIC_URL = "https://agent-hooks.sammohr.dev"


class ConfigError(Exception):
    """The environment cannot produce a usable receiver."""


@dataclass(frozen=True)
class Config:
    data_dir: Path
    address: str
    port: int
    gateway_url: str
    gateway_token: str
    admin_token: str
    public_url: str


def _read_token(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"{label}: cannot read {path} ({exc.strerror})") from exc
    if not value:
        raise ConfigError(f"{label}: {path} is empty")
    return value


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    port_text = env.get("WEBHOOK_LISTEN_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError(f"WEBHOOK_LISTEN_PORT is not an integer: {port_text!r}") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"WEBHOOK_LISTEN_PORT is out of range: {port}")
    public_url = env.get("WEBHOOK_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")
    if not public_url.startswith(("https://", "http://")):
        raise ConfigError(f"WEBHOOK_PUBLIC_URL is not an http(s) URL: {public_url!r}")
    return Config(
        data_dir=Path(env.get("WEBHOOK_DATA_DIR", DEFAULT_DATA_DIR)),
        address=env.get("WEBHOOK_LISTEN_ADDR", DEFAULT_ADDRESS),
        port=port,
        gateway_url=env.get("WEBHOOK_GATEWAY_URL", DEFAULT_GATEWAY_URL),
        gateway_token=_read_token(
            Path(env.get("WEBHOOK_GATEWAY_TOKEN_FILE", DEFAULT_GATEWAY_TOKEN_FILE)), "enqueue token"
        ),
        admin_token=_read_token(
            Path(env.get("WEBHOOK_ADMIN_TOKEN_FILE", DEFAULT_ADMIN_TOKEN_FILE)), "admin token"
        ),
        public_url=public_url,
    )
