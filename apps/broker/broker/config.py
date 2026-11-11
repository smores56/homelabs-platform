"""Enrollment parsing. The config file is non-secret; credentials are files.

The enrollment file describes *what* the broker may reach and *which* credential
header it injects. The credential values live beside the pod at
`/run/secrets/<name>` (one key per file, from the SOPS/ESO Secret), are read once
at start, and are never written to the config, a log, or a response.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Logical host names become the first path segment, so they are kept boring.
LOGICAL_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Header values are written to the wire behind a newline; a newline in one is a
# request-splitting bug, not a configuration option.
HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
HEADER_VALUE_FORBIDDEN = ("\r", "\n", "\0")
SECRET_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
INJECTION_KINDS = frozenset({"bearer", "header", "basic", "none"})
# Enrollment headers may not smuggle a credential of their own; the only
# credential on the wire is the one named by `inject`.
CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "private-token",
        "x-github-token",
    }
)


class ConfigError(ValueError):
    """The enrollment file or the secret directory cannot be trusted."""


@dataclass(frozen=True)
class Injection:
    kind: str
    header: str = "Authorization"
    username: str = ""


@dataclass(frozen=True)
class Enrollment:
    name: str
    scheme: str
    host: str
    port: int
    allow_private: bool
    secret_name: str
    injection: Injection
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def default_port(self) -> bool:
        return (self.scheme == "https" and self.port == 443) or (self.scheme == "http" and self.port == 80)

    @property
    def host_header(self) -> str:
        return self.host if self.default_port else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    deny_addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
    hosts: dict[str, Enrollment]


def _parse_injection(logical: str, raw: object) -> Injection:
    if raw is None:
        return Injection(kind="none")
    if not isinstance(raw, dict):
        raise ConfigError(f"{logical}: inject must be an object")
    kind = raw.get("kind", "")
    if kind not in INJECTION_KINDS:
        raise ConfigError(f"{logical}: unknown injection kind {kind!r}")
    header = raw.get("header", "Authorization")
    username = raw.get("username", "")
    if not isinstance(header, str) or not HEADER_NAME.match(header):
        raise ConfigError(f"{logical}: injection header is not a header name")
    if header.lower() not in {"authorization"} and kind in {"bearer", "basic"}:
        raise ConfigError(f"{logical}: {kind} injection must use Authorization")
    if kind == "basic" and (not isinstance(username, str) or not username):
        raise ConfigError(f"{logical}: basic injection needs a username")
    return Injection(kind=kind, header=header, username=username)


def _parse_enrollment(logical: str, raw: object) -> Enrollment:
    if not LOGICAL_HOST.match(logical):
        raise ConfigError(f"invalid logical host {logical!r}")
    if not isinstance(raw, dict):
        raise ConfigError(f"{logical}: enrollment must be an object")

    scheme = raw.get("scheme", "")
    if scheme not in {"http", "https"}:
        raise ConfigError(f"{logical}: scheme must be http or https")
    host = raw.get("host", "")
    if not isinstance(host, str) or not host:
        raise ConfigError(f"{logical}: host is required")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # An IP-literal enrollment would skip DNS entirely, which is exactly the
        # path the address policy exists to control.
        raise ConfigError(f"{logical}: host must be a DNS name, not an IP literal")
    port = raw.get("port", 443 if scheme == "https" else 80)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"{logical}: port must be 1..65535")

    allow_private = raw.get("allow_private", False)
    if not isinstance(allow_private, bool):
        raise ConfigError(f"{logical}: allow_private must be a boolean")

    injection = _parse_injection(logical, raw.get("inject"))
    secret_name = raw.get("secret", "")
    if injection.kind == "none":
        if secret_name:
            raise ConfigError(f"{logical}: no-injection enrollment must not name a secret")
    else:
        if not isinstance(secret_name, str) or not SECRET_NAME.match(secret_name):
            raise ConfigError(f"{logical}: credentialed enrollment needs a secret file name")
        if injection.kind != "none" and scheme != "https" and not allow_private:
            # In-cluster HTTP is the one exception: an explicitly private
            # enrollment (the Loki gateway, the status exporter) never leaves
            # the cluster network, and its credential is already cluster-local.
            raise ConfigError(f"{logical}: credential injection requires https")

    headers: list[tuple[str, str]] = []
    raw_headers = raw.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise ConfigError(f"{logical}: headers must be an object")
    for name, value in raw_headers.items():
        if not HEADER_NAME.match(name):
            raise ConfigError(f"{logical}: invalid header name {name!r}")
        if name.lower() in CREDENTIAL_HEADERS:
            raise ConfigError(f"{logical}: {name} is a credential header; use inject")
        if name.lower() == injection.header.lower():
            raise ConfigError(f"{logical}: {name} would overwrite the injected credential")
        if not isinstance(value, str) or any(bad in value for bad in HEADER_VALUE_FORBIDDEN):
            raise ConfigError(f"{logical}: invalid value for header {name}")
        headers.append((name, value))

    return Enrollment(
        name=logical,
        scheme=scheme,
        host=host,
        port=port,
        allow_private=allow_private,
        secret_name=secret_name,
        injection=injection,
        headers=tuple(headers),
    )


def _parse_address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        raise ConfigError(f"deny_addresses holds a non-address {text!r}") from None


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    try:
        raw = json.loads(Path(path).read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read enrollment file {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"enrollment file is not JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("enrollment file must be a JSON object")

    listen = env.get("BROKER_LISTEN", raw.get("listen", ""))
    listen_host, separator, listen_port = listen.rpartition(":")
    if not separator or not listen_host:
        raise ConfigError(f"listen must be host:port, got {listen!r}")
    try:
        port = int(listen_port)
    except ValueError:
        raise ConfigError(f"listen port is not a number: {listen!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"listen port must be 1..65535: {listen!r}")

    raw_hosts = raw.get("logical_hosts", {})
    if not isinstance(raw_hosts, dict) or not raw_hosts:
        raise ConfigError("logical_hosts must be a non-empty object")
    hosts = {name: _parse_enrollment(name, value) for name, value in raw_hosts.items()}

    deny = [_parse_address(item) for item in raw.get("deny_addresses", [])]
    # A node IP outside the private ranges (a public-cloud node) has to be
    # refused explicitly; env keeps it a per-deployment fact, not a repo edit.
    for item in env.get("BROKER_DENY_ADDRESSES", "").replace(",", " ").split():
        deny.append(_parse_address(item))

    return Config(listen_host=listen_host, listen_port=port, deny_addresses=tuple(deny), hosts=hosts)


def load_secrets(hosts: dict[str, Enrollment], directory: str | Path) -> dict[str, str]:
    """Read every named credential once. A missing or empty one is fatal."""
    base = Path(directory)
    secrets: dict[str, str] = {}
    for enrollment in hosts.values():
        name = enrollment.secret_name
        if not name or name in secrets:
            continue
        try:
            value = Path(base, name).read_text().strip()
        except OSError as exc:
            raise ConfigError(f"{enrollment.name}: cannot read secret {name}: {exc}") from None
        if not value:
            raise ConfigError(f"{enrollment.name}: secret {name} is empty")
        secrets[name] = value
    return secrets
