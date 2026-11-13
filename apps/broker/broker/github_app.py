"""Mint GitHub App installation tokens, scoped per logical host.

The broker holds the app's private key; the agent never sees a GitHub
credential. Each enrollment names the installation and the exact repository and
permission subset it needs, so a token minted for `github-apps` cannot write to
the platform repository even though the app is installed there.

No third-party import at module load: `_sign_rs256` imports `cryptography`
lazily, and `tests/test_github_app.py` injects a fake signer, so the unit suite
keeps running on the stdlib.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

API = "https://api.github.com"
# GitHub rejects a JWT older than 60 s and one that lives longer than 10 min.
JWT_BACKDATE = 60
JWT_TTL = 540
# Refresh this long before the installation token expires; the broker is the
# only user, so a small margin is enough.
REFRESH_MARGIN = 300

Signer = Callable[[bytes], bytes]
# (url, jwt, body) -> (status, parsed json). Injected in tests.
Transport = Callable[[str, str, Mapping[str, object]], tuple[int, object]]


class CredentialUnavailable(RuntimeError):
    """The app could not mint a token; the request must fail closed."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign_rs256(private_key_pem: bytes, message: bytes) -> bytes:
    # Imported here so the import and the unit suite stay dependency-free.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def _post(url: str, jwt: str, body: Mapping[str, object]) -> tuple[int, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "homelabs-broker",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (OSError, ValueError):
        return 0, {}


@dataclass(frozen=True)
class _Cached:
    token: str
    expires_at: float


class GithubAppTokens:
    def __init__(
        self,
        app_id: int,
        private_key: bytes,
        *,
        signer: Signer | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._key = private_key
        self._signer = signer or (lambda message: _sign_rs256(private_key, message))
        self._transport = transport or _post
        self._clock = clock
        self._cache: dict[tuple, _Cached] = {}
        self._lock = threading.Lock()

    def jwt(self) -> str:
        now = int(self._clock())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {"iat": now - JWT_BACKDATE, "exp": now + JWT_TTL, "iss": self._app_id}
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
        )
        return f"{signing_input}.{_b64url(self._signer(signing_input.encode()))}"

    def token(self, installation: int, repositories: tuple[str, ...], permissions: Mapping[str, str]) -> str:
        key = (installation, tuple(sorted(repositories)), tuple(sorted(permissions.items())))
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at - REFRESH_MARGIN > now:
                return cached.token
        body: dict[str, object] = {"permissions": dict(permissions)}
        if repositories:
            body["repositories"] = list(repositories)
        status, reply = self._transport(
            f"{API}/app/installations/{installation}/access_tokens", self.jwt(), body
        )
        if status not in (200, 201) or not isinstance(reply, dict):
            raise CredentialUnavailable(f"github-app: installation {installation} token request failed ({status})")
        token = reply.get("token")
        expires = reply.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            raise CredentialUnavailable(f"github-app: installation {installation} returned no token")
        try:
            expires_at = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            raise CredentialUnavailable(f"github-app: installation {installation} returned a bad expiry") from None
        with self._lock:
            self._cache[key] = _Cached(token=token, expires_at=expires_at)
        return token

    def active_tokens(self) -> list[str]:
        with self._lock:
            return [entry.token for entry in self._cache.values()]
