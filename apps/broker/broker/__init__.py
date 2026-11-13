"""Credential-injecting broker for the cluster agent (sdlc #582).

The broker is the only place a logical host's credential exists. It maps an
enrolled logical host to one real endpoint, resolves and pins the address
itself, strips every credential the agent sent, injects exactly one enrolled
credential, never follows a redirect, and logs metadata plus byte counts only.

Static credentials are files; GitHub App enrollments mint short-lived scoped
tokens (`github_app.py`). The only third-party dependency is the RSA signer,
imported lazily so the unit tests still run on the stdlib.
"""

from .config import (
    Config,
    ConfigError,
    Enrollment,
    GithubAppConfig,
    Injection,
    load_app_signing,
    load_config,
    load_secrets,
)
from .credentials import AppCredentials, StaticCredentials
from .github_app import CredentialUnavailable, GithubAppTokens
from .policy import Denied, PinnedAddress, address_allowed, check_redirect, resolve_pinned, split_target
from .redact import redact, secret_variants
from .server import Broker, make_handler, make_server

__all__ = [
    "Broker",
    "Config",
    "ConfigError",
    "Denied",
    "Enrollment",
    "GithubAppConfig",
    "Injection",
    "PinnedAddress",
    "address_allowed",
    "check_redirect",
    "load_app_signing",
    "load_config",
    "load_secrets",
    "make_handler",
    "make_server",
    "redact",
    "resolve_pinned",
    "secret_variants",
    "AppCredentials",
    "StaticCredentials",
    "CredentialUnavailable",
    "GithubAppTokens",
    "split_target",
]
