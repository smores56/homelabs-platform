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
GITHUB_PERMISSION = re.compile(r"^[a-z][a-z_]*$")
PERMISSION_LEVELS = frozenset({"read", "write", "admin"})
REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
INJECTION_KINDS = frozenset({"bearer", "header", "basic", "none"})
# The request contract: the methods the front end dispatches and the hard body
# cap. The broker's own enrollment may lower the cap per logical host.
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MAX_REQUEST_BYTES = 64 * 1024 * 1024
# A body guard names one JSON field by dot-separated keys.
BODY_FIELD = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
PATH_PREFIX = re.compile(r"^(/[A-Za-z0-9._~-]+)+/?$")
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
class AppScope:
    """One logical host's slice of the app: an installation plus what it may do.

    `installation` is the *secret file name* that holds the numeric id, so a
    reinstall does not need a commit.
    """

    installation: str
    repositories: tuple[str, ...]
    permissions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class GithubAppConfig:
    """Non-secret names of the two files the operator supplies."""

    key_secret: str = "github-app-private-key"
    id_secret: str = "github-app-id"


@dataclass(frozen=True)
class BodyGuard:
    """A conditional body predicate.

    When a request matches `methods` (empty means any) and any `paths` regex,
    the body must be JSON whose `field` is a string matching `allow`.
    """

    methods: tuple[str, ...]
    paths: tuple[re.Pattern[str], ...]
    field: str
    allow: re.Pattern[str]


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
    app: AppScope | None = None
    # Request predicates (#624). Empty tuples and None preserve the original
    # behavior: every method and path was forwarded to every enrollment.
    allow_methods: tuple[str, ...] = ()
    deny_methods: tuple[str, ...] = ()
    allow_paths: tuple[re.Pattern[str], ...] = ()
    deny_paths: tuple[re.Pattern[str], ...] = ()
    deny_body_patterns: tuple[re.Pattern[str], ...] = ()
    body_guards: tuple[BodyGuard, ...] = ()
    max_body_bytes: int | None = None
    # A host whose provider is not provisioned yet: a missing secret file is a
    # 503 for that host instead of a startup abort. Never a request without it.
    optional_secret: bool = False
    # Prepended to the caller's path before forwarding. The caller addresses
    # $BROKER_URL/<logical>/<path>; without this the upstream only ever sees
    # <path>, so a service whose own routes are namespaced (agent-control's
    # /self/... and /cluster/...) needs its namespace restored here.
    path_prefix: str = ""

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
    github_app: GithubAppConfig | None = None


def _parse_app_scope(logical: str, raw: object) -> AppScope:
    if not isinstance(raw, dict):
        raise ConfigError(f"{logical}: github_app must be an object")
    unknown = set(raw) - {"installation", "repositories", "permissions"}
    if unknown:
        raise ConfigError(f"{logical}: unknown github_app keys {sorted(unknown)}")
    installation = raw.get("installation")
    if not isinstance(installation, str) or not SECRET_NAME.match(installation):
        raise ConfigError(f"{logical}: github_app.installation must be a secret file name")
    repositories = raw.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ConfigError(f"{logical}: github_app.repositories must be a non-empty list")
    if not all(isinstance(repo, str) and REPOSITORY_NAME.match(repo) for repo in repositories):
        raise ConfigError(f"{logical}: github_app.repositories must be repository names")
    permissions = raw.get("permissions")
    if not isinstance(permissions, dict) or not permissions:
        raise ConfigError(f"{logical}: github_app.permissions must be a non-empty object")
    for key, level in permissions.items():
        if not isinstance(key, str) or not GITHUB_PERMISSION.match(key):
            raise ConfigError(f"{logical}: bad github_app permission {key!r}")
        if level not in PERMISSION_LEVELS:
            raise ConfigError(f"{logical}: github_app permission {key} must be read, write or admin")
    return AppScope(
        installation=installation,
        repositories=tuple(repositories),
        permissions=tuple(sorted(permissions.items())),
    )


def _parse_app_config(raw: object) -> GithubAppConfig:
    if not isinstance(raw, dict):
        raise ConfigError("github_app must be an object")
    unknown = set(raw) - {"key_secret", "id_secret"}
    if unknown:
        raise ConfigError(f"github_app: unknown keys {sorted(unknown)}")
    key_secret = raw.get("key_secret", "github-app-private-key")
    id_secret = raw.get("id_secret", "github-app-id")
    for name in (key_secret, id_secret):
        if not isinstance(name, str) or not SECRET_NAME.match(name):
            raise ConfigError("github_app file names must be secret file names")
    if key_secret == id_secret:
        raise ConfigError("github_app key_secret and id_secret must differ")
    return GithubAppConfig(key_secret=key_secret, id_secret=id_secret)


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


def _parse_methods(logical: str, key: str, raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{logical}: {key} must be a list of methods")
    methods: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError(f"{logical}: {key} must be a list of methods")
        method = item.upper()
        if method not in METHODS:
            raise ConfigError(f"{logical}: {key} names an unsupported method {item!r}")
        if method not in methods:
            methods.append(method)
    return tuple(methods)


def _parse_patterns(logical: str, key: str, raw: object) -> tuple[re.Pattern[str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{logical}: {key} must be a list of regex strings")
    patterns: list[re.Pattern[str]] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ConfigError(f"{logical}: {key} must be a list of non-empty regex strings")
        try:
            patterns.append(re.compile(item))
        except re.error:
            raise ConfigError(f"{logical}: {key} regex {item!r} does not compile") from None
    return tuple(patterns)


def _parse_body_guard(logical: str, raw: object) -> BodyGuard:
    if not isinstance(raw, dict):
        raise ConfigError(f"{logical}: body_guards entries must be objects")
    unknown = set(raw) - {"methods", "paths", "field", "allow"}
    if unknown:
        raise ConfigError(f"{logical}: unknown body guard keys {sorted(unknown)}")
    paths = _parse_patterns(logical, "body_guards.paths", raw.get("paths"))
    if not paths:
        raise ConfigError(f"{logical}: a body guard needs at least one path regex")
    field = raw.get("field")
    if not isinstance(field, str) or not BODY_FIELD.match(field):
        raise ConfigError(f"{logical}: a body guard field must be a dot-separated JSON path")
    allow_raw = raw.get("allow")
    if not isinstance(allow_raw, str) or not allow_raw:
        raise ConfigError(f"{logical}: a body guard needs an allow regex")
    try:
        allow = re.compile(allow_raw)
    except re.error:
        raise ConfigError(f"{logical}: body guard allow regex {allow_raw!r} does not compile") from None
    return BodyGuard(
        methods=_parse_methods(logical, "body_guards.methods", raw.get("methods")),
        paths=paths,
        field=field,
        allow=allow,
    )


def _parse_body_guards(logical: str, raw: object) -> tuple[BodyGuard, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{logical}: body_guards must be a list")
    return tuple(_parse_body_guard(logical, item) for item in raw)


def _parse_max_body_bytes(logical: str, raw: object) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= MAX_REQUEST_BYTES:
        raise ConfigError(f"{logical}: max_body_bytes must be 1..{MAX_REQUEST_BYTES}")
    return raw


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
    app = _parse_app_scope(logical, raw["github_app"]) if "github_app" in raw else None
    if app is not None and secret_name:
        raise ConfigError(f"{logical}: github_app and secret are mutually exclusive")
    if app is not None:
        if injection.kind not in {"bearer", "basic"}:
            raise ConfigError(f"{logical}: github_app requires bearer or basic injection")
        if scheme != "https":
            raise ConfigError(f"{logical}: github_app requires https")
    optional_secret = raw.get("optional_secret", False)
    if not isinstance(optional_secret, bool):
        raise ConfigError(f"{logical}: optional_secret must be a boolean")
    if optional_secret and (not secret_name or app is not None):
        raise ConfigError(f"{logical}: optional_secret needs a static secret, not a github_app")
    path_prefix = raw.get("path_prefix", "")
    if not isinstance(path_prefix, str) or (path_prefix and not PATH_PREFIX.match(path_prefix)):
        raise ConfigError(f"{logical}: path_prefix must be empty or a leading-slash path")
    if injection.kind == "none":
        if secret_name:
            raise ConfigError(f"{logical}: no-injection enrollment must not name a secret")
    elif app is None:
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

    allow_methods = _parse_methods(logical, "allow_methods", raw.get("allow_methods"))
    deny_methods = _parse_methods(logical, "deny_methods", raw.get("deny_methods"))
    overlap = sorted(set(allow_methods) & set(deny_methods))
    if overlap:
        raise ConfigError(f"{logical}: methods both allowed and denied: {','.join(overlap)}")

    return Enrollment(
        name=logical,
        scheme=scheme,
        host=host,
        port=port,
        allow_private=allow_private,
        secret_name=secret_name,
        injection=injection,
        headers=tuple(headers),
        app=app,
        allow_methods=allow_methods,
        deny_methods=deny_methods,
        allow_paths=_parse_patterns(logical, "allow_paths", raw.get("allow_paths")),
        deny_paths=_parse_patterns(logical, "deny_paths", raw.get("deny_paths")),
        deny_body_patterns=_parse_patterns(logical, "deny_body_patterns", raw.get("deny_body_patterns")),
        body_guards=_parse_body_guards(logical, raw.get("body_guards")),
        max_body_bytes=_parse_max_body_bytes(logical, raw.get("max_body_bytes")),
        optional_secret=optional_secret,
        path_prefix=path_prefix.rstrip("/"),
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

    raw_app = raw.get("github_app")
    github_app = _parse_app_config(raw_app) if raw_app is not None else None
    consumers = [name for name, enrollment in hosts.items() if enrollment.app]
    if consumers and github_app is None:
        raise ConfigError(f"enrollments need a top-level github_app: {sorted(consumers)}")
    if github_app is not None and not consumers:
        raise ConfigError("github_app is configured but no enrollment uses it")

    deny = [_parse_address(item) for item in raw.get("deny_addresses", [])]
    # A node IP outside the private ranges (a public-cloud node) has to be
    # refused explicitly; env keeps it a per-deployment fact, not a repo edit.
    for item in env.get("BROKER_DENY_ADDRESSES", "").replace(",", " ").split():
        deny.append(_parse_address(item))

    return Config(
        listen_host=listen_host,
        listen_port=port,
        deny_addresses=tuple(deny),
        hosts=hosts,
        github_app=github_app,
    )


def load_secrets(hosts: dict[str, Enrollment], directory: str | Path) -> dict[str, str]:
    """Read every named credential once. A missing or empty one is fatal,
    except for a host that declares optional_secret: it serves 503 until the
    file appears instead of taking the whole broker down."""
    base = Path(directory)
    secrets: dict[str, str] = {}
    for enrollment in hosts.values():
        name = enrollment.secret_name
        if not name or name in secrets:
            continue
        try:
            value = Path(base, name).read_text().strip()
        except OSError as exc:
            if enrollment.optional_secret:
                continue
            raise ConfigError(f"{enrollment.name}: cannot read secret {name}: {exc}") from None
        if not value:
            if enrollment.optional_secret:
                continue
            raise ConfigError(f"{enrollment.name}: secret {name} is empty")
        secrets[name] = value
    return secrets


@dataclass(frozen=True)
class AppSigning:
    app_id: int
    private_key: bytes
    # secret file name -> numeric installation id
    installations: dict[str, int]


def _read_integer(base: Path, name: str, label: str) -> int:
    try:
        text = Path(base, name).read_text().strip()
    except OSError as exc:
        raise ConfigError(f"github-app: cannot read {label} {name}: {exc}") from None
    if not text.isdigit() or int(text) <= 0:
        raise ConfigError(f"github-app: {name} must be a positive integer")
    return int(text)


def load_app_signing(config: Config, directory: str | Path) -> AppSigning | None:
    """Read the app id, private key and installation ids once; any gap is fatal."""
    if config.github_app is None:
        return None
    base = Path(directory)
    app_id = _read_integer(base, config.github_app.id_secret, "app id")
    key_name = config.github_app.key_secret
    try:
        key = Path(base, key_name).read_bytes()
    except OSError as exc:
        raise ConfigError(f"github-app: cannot read private key {key_name}: {exc}") from None
    if b"PRIVATE KEY-----" not in key or b"-----BEGIN" not in key:
        raise ConfigError(f"github-app: {key_name} is not a PEM private key")
    installations: dict[str, int] = {}
    for enrollment in config.hosts.values():
        if enrollment.app is not None:
            name = enrollment.app.installation
            installations[name] = _read_integer(base, name, "installation id")
    return AppSigning(app_id=app_id, private_key=key, installations=installations)
