"""Credential resolution for the two enrollment kinds.

Static enrollments read a file from the projected Secret; GitHub App
enrollments mint a short-lived, scoped installation token. The broker's
redaction set includes every static value and every token currently minted.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from .config import Enrollment
from .github_app import GithubAppTokens


class Credentials(Protocol):
    def value(self, enrollment: Enrollment) -> str: ...

    def values(self) -> dict[str, str]: ...


class StaticCredentials:
    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)

    def value(self, enrollment: Enrollment) -> str:
        if not enrollment.secret_name:
            return ""
        return self._secrets.get(enrollment.secret_name, "")

    def values(self) -> dict[str, str]:
        return dict(self._secrets)


class AppCredentials:
    """Static files plus app-minted tokens, behind one lookup."""

    def __init__(
        self,
        secrets: Mapping[str, str],
        tokens: GithubAppTokens,
        installations: Mapping[str, int],
    ) -> None:
        self._static = StaticCredentials(secrets)
        self._tokens = tokens
        self._installations = dict(installations)

    def value(self, enrollment: Enrollment) -> str:
        scope = enrollment.app
        if scope is None:
            return self._static.value(enrollment)
        return self._tokens.token(
            self._installations[scope.installation], scope.repositories, dict(scope.permissions)
        )

    def values(self) -> dict[str, str]:
        merged = self._static.values()
        for index, token in enumerate(self._tokens.active_tokens()):
            # Names never leave the process; only the values matter for redaction.
            merged[f"minted-token-{index}"] = token
        return merged
