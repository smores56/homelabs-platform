"""GitHub backup plans and manifests: argv-only, offline, and verifiable.

The plan never clones anything. It renders the argv a caller may run, keeping
``shell=True`` out of the picture entirely. The manifest is the durable record
of what one run captured, and ``verify_bundle`` checks a bundle with git itself
rather than trusting the filename.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from ..errors import ContractError, ManifestError, ValidationError

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FIELDS = ("repo", "commit", "ref_count", "bundle_size", "captured_at")
_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GITHUB = "https://github.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be an ISO 8601 string")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise ValidationError(f"{label} is not ISO 8601: {value!r}") from error
    return value


def _count(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer, got {value!r}")
    return value


@dataclass(frozen=True)
class GithubBackupPlan:
    """What to capture from one GitHub owner: mirrors and optional bundles."""

    owner: str
    repos: tuple[str, ...]
    out_dir: str
    mirror: bool = True
    bundle: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "repos", tuple(self.repos))

        if not isinstance(self.owner, str) or not _OWNER_PATTERN.fullmatch(self.owner):
            raise ContractError(f"GitHub owner is not a plain account name: {self.owner!r}")
        if not self.repos:
            raise ContractError("GitHub backup plan has no repositories")
        for repo in self.repos:
            if not isinstance(repo, str) or not _REPO_PATTERN.fullmatch(repo):
                raise ContractError(f"repository name is invalid: {repo!r}")
        if len(set(self.repos)) != len(self.repos):
            raise ContractError("GitHub backup plan has duplicate repositories")
        if not isinstance(self.out_dir, str) or not os.path.isabs(self.out_dir):
            raise ContractError(
                f"GitHub backup out_dir must be absolute, got {self.out_dir!r}"
            )
        for flag, name in ((self.mirror, "mirror"), (self.bundle, "bundle")):
            if not isinstance(flag, bool):
                raise ContractError(f"GitHub backup {name} must be a bool")
        if not self.mirror and not self.bundle:
            raise ContractError("GitHub backup plan has no mirror and no bundle")
        if self.bundle and not self.mirror:
            raise ContractError("a bundle is created from the mirror, so mirror must be enabled")

    def mirror_path(self, repo: str) -> str:
        return os.path.join(self.out_dir, f"{repo}.git")

    def bundle_path(self, repo: str) -> str:
        return os.path.join(self.out_dir, f"{repo}.bundle")

    def commands(self) -> list[list[str]]:
        """Every command as a list argv, ordered by repository name."""

        commands: list[list[str]] = []
        for repo in sorted(self.repos):
            destination = self.mirror_path(repo)
            commands.append(
                [
                    "git",
                    "clone",
                    "--mirror",
                    f"{_GITHUB}/{self.owner}/{repo}.git",
                    destination,
                ]
            )
            if self.bundle:
                commands.append(
                    [
                        "git",
                        "-C",
                        destination,
                        "bundle",
                        "create",
                        self.bundle_path(repo),
                        "--all",
                    ]
                )
        return commands


@dataclass(frozen=True)
class PythonGithubBackupPlan:
    """Argv for python-github-backup, which captures more than a git mirror.

    The token is never placed on argv; the tool reads ``GITHUB_TOKEN`` from the
    environment. That keeps it out of the process table and logs.
    """

    owner: str
    out_dir: str
    repositories: bool = True
    forks: bool = False
    lfs: bool = True
    gists: bool = True
    issues: bool = True
    issue_comments: bool = True
    pulls: bool = True
    pull_comments: bool = True
    releases: bool = True
    wikis: bool = True
    discussions: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not _OWNER_PATTERN.fullmatch(self.owner):
            raise ContractError(f"GitHub owner is not a plain account name: {self.owner!r}")
        if not isinstance(self.out_dir, str) or not os.path.isabs(self.out_dir):
            raise ContractError(
                f"GitHub backup out_dir must be absolute, got {self.out_dir!r}"
            )
        flags = (
            self.repositories,
            self.forks,
            self.lfs,
            self.gists,
            self.issues,
            self.issue_comments,
            self.pulls,
            self.pull_comments,
            self.releases,
            self.wikis,
            self.discussions,
        )
        if not any(flags):
            raise ContractError("python-github-backup plan selects nothing to capture")
        if any(not isinstance(flag, bool) for flag in flags):
            raise ContractError("python-github-backup flags must be bools")

    def commands(self) -> list[list[str]]:
        argv = [
            "python-github-backup",
            self.owner,
            "--output-directory",
            self.out_dir,
        ]
        for flag, enabled in (
            ("--repositories", self.repositories),
            ("--forks", self.forks),
            ("--lfs", self.lfs),
            ("--gists", self.gists),
            ("--issues", self.issues),
            ("--issue-comments", self.issue_comments),
            ("--pulls", self.pulls),
            ("--pull-comments", self.pull_comments),
            ("--releases", self.releases),
            ("--wikis", self.wikis),
            ("--discussions", self.discussions),
        ):
            if enabled:
                argv.append(flag)
        return [argv]


def _entry(payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValidationError("GitHub manifest entry must be a mapping")
    missing = [field for field in MANIFEST_FIELDS if field not in payload]
    if missing:
        raise ValidationError(
            f"GitHub manifest entry is missing {', '.join(sorted(missing))}"
        )

    repo = payload["repo"]
    if not isinstance(repo, str) or not _REPO_PATTERN.fullmatch(repo):
        raise ValidationError(f"GitHub manifest repo is invalid: {repo!r}")
    commit = payload["commit"]
    if not isinstance(commit, str) or not commit.strip():
        raise ValidationError(f"GitHub manifest commit for {repo!r} is empty")

    captured_at = payload["captured_at"]
    if isinstance(captured_at, str) and captured_at.strip():
        captured_at = _timestamp(captured_at, f"GitHub manifest captured_at for {repo!r}")
    elif captured_at is None or captured_at == "":
        captured_at = _now()
    else:
        raise ValidationError(f"GitHub manifest captured_at for {repo!r} is invalid")

    return {
        "repo": repo,
        "commit": commit,
        "ref_count": _count(payload["ref_count"], f"GitHub manifest ref_count for {repo!r}"),
        "bundle_size": _count(
            payload["bundle_size"], f"GitHub manifest bundle_size for {repo!r}"
        ),
        "captured_at": captured_at,
    }


def _entries(results: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    entries = [_entry(payload) for payload in results]
    repos = [entry["repo"] for entry in entries]
    if len(set(repos)) != len(repos):
        raise ValidationError("GitHub manifest records a repository more than once")
    entries.sort(key=lambda entry: entry["repo"])
    return entries


def _write(path: Path, results: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    entries = _entries(results)
    document = {"schema_version": MANIFEST_SCHEMA_VERSION, "repos": entries}
    body = json.dumps(document, indent=2, sort_keys=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return entries


def _read(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(f"GitHub manifest {path} does not exist") from error
    except (OSError, ValueError) as error:
        raise ManifestError(f"GitHub manifest {path} is unreadable: {error}") from error

    if not isinstance(payload, Mapping):
        raise ManifestError(f"GitHub manifest {path} is not an object")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"GitHub manifest {path} has schema version {payload.get('schema_version')!r}, "
            f"not {MANIFEST_SCHEMA_VERSION}"
        )
    repos = payload.get("repos")
    if not isinstance(repos, list):
        raise ManifestError(f"GitHub manifest {path} has no repos list")
    try:
        return _entries(repos)
    except ValidationError as error:
        raise ManifestError(f"GitHub manifest {path} is invalid: {error}") from error


def manifest(
    path: str | os.PathLike[str],
    results: Iterable[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Write ``results`` to a manifest, or read the manifest when omitted."""

    location = Path(path)
    return _read(location) if results is None else _write(location, results)


def verify_bundle(path: str | os.PathLike[str]) -> bool:
    """Ask git whether ``path`` is a complete, verifiable bundle."""

    argv = ["git", "bundle", "verify", os.fspath(path)]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except (OSError, ValueError):
        return False
    return completed.returncode == 0
