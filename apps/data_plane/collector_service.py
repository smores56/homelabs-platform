"""One-source collector: fetch, normalize, validate, and submit to a relay.

A collector process is bound to exactly one source. It reads that source's rows
through an injectable fetcher (the tests use a plain dict, production uses a
stdlib HTTP GET against the source's proxy), normalizes them with the source's
adapter when one exists, validates the result against the schema descriptor, and
submits each record to a :class:`~data_plane.collectors.relay.Relay` with the
relay's HMAC signature, a stable idempotency key, and a monotonic cursor.

The source id and the schema descriptor must agree: a schema that declares some
other source is refused at construction, never at the first record. A fetch or
schema failure aborts the whole pass before anything is submitted, so a partial
or unvalidated run cannot reach the relay.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from data_plane.collectors.adapters import NORMALIZERS, NormalizedRecord
from data_plane.collectors.relay import IDEMPOTENCY_HEADER, SIGNATURE_HEADER, Relay
from data_plane.collectors.schemas import (
    SchemaDescriptor,
    load_descriptor,
    load_descriptor_from_file,
)
from data_plane.errors import ValidationError
from data_plane.identity import canonical_json

Fetcher = Callable[[str], Iterable[Mapping[str, Any]]]
Submitter = Callable[[Mapping[str, str], bytes, int], Mapping[str, Any]]
Normalizer = Callable[..., NormalizedRecord]

_FETCH_TIMEOUT_SECONDS = 10.0
_MAX_IDEMPOTENCY_KEY = 200

# Collector Deployments name a source by its provider short name, while the
# adapter map is keyed by descriptor name. The two agree for most sources; tuta
# is the one mismatch worth carrying explicitly.
_SOURCE_ADAPTERS = {
    "tuta": "mail",
    "mail": "mail",
    "finance": "finance",
    "brave": "brave",
    "immich": "immich",
    "calendar": "calendar",
    "phone": "phone",
}


class CollectorError(ValidationError):
    """A collector cannot fetch, normalize, or validate a record."""


def _check_http_url(url: str, label: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise CollectorError(f"{label} is required")
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CollectorError(f"{label} must be an http(s) URL")
    return url.strip()


def _reject_credentials(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise CollectorError("proxy URL must not carry credentials")


def fetch_json(url: str, *, timeout: float = _FETCH_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Default fetcher: one HTTP GET returning a JSON array or ``{"records": []}``.

    It is injectable so the tests never open a socket. Any transport, status, or
    decoding failure propagates as ``CollectorError`` from the caller, which
    aborts the pass before submission.
    """

    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError("proxy response is not valid UTF-8 JSON") from exc
    if isinstance(value, dict):
        value = value.get("records")
    if not isinstance(value, list):
        raise CollectorError("proxy response must be a JSON array or {'records': [...]}")
    return value


def load_descriptor_source(
    source_or_path: str, *, name: str | None = None
) -> SchemaDescriptor:
    """Load a descriptor from a packaged name or a mounted JSON file."""

    path = Path(source_or_path)
    if path.is_file():
        return load_descriptor_from_file(path, name=name)
    requested = name or source_or_path
    return load_descriptor(_SOURCE_ADAPTERS.get(requested, requested))


def _normalizer_for(
    source_id: str, descriptor: SchemaDescriptor
) -> Normalizer | None:
    # Adapter keys are the descriptor's short name ("brave") or a provider alias
    # ("tuta" -> "mail"), not always the record source id ("tuta.mail").
    for candidate in (
        descriptor.name,
        source_id,
        _SOURCE_ADAPTERS.get(descriptor.name),
        _SOURCE_ADAPTERS.get(source_id),
        descriptor.source_id,
    ):
        if candidate is not None and candidate in NORMALIZERS:
            return NORMALIZERS[candidate]
    return None


class Collector:
    """One source's fetch → normalize → validate → relay pipeline."""

    def __init__(
        self,
        *,
        source_id: str,
        descriptor: SchemaDescriptor,
        proxy_url: str,
        secret: bytes | str | None = None,
        relay: Relay | None = None,
        fetcher: Fetcher | None = None,
        sink: Callable[[dict[str, Any]], Any] | None = None,
        normalizer: Normalizer | None = None,
        submitter: Submitter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        descriptor.validate()
        canonical = _SOURCE_ADAPTERS.get(source_id)
        if source_id not in {descriptor.name, descriptor.source_id} and canonical != descriptor.name:
            raise CollectorError(
                f"source {source_id!r} does not match schema {descriptor.name!r} "
                f"for {descriptor.source_id!r}"
            )
        self._source_id = source_id
        self._descriptor = descriptor
        self._proxy_url = _check_http_url(proxy_url, "source proxy URL")
        _reject_credentials(self._proxy_url)
        self._fetcher: Fetcher = fetch_json if fetcher is None else fetcher
        self._clock = clock
        self.accepted: list[dict[str, Any]] = []

        if relay is None:
            if secret is None:
                raise CollectorError("a collector needs a relay or a relay secret")
            relay = Relay(
                secret,
                descriptor.relay_schema(),
                sink if sink is not None else self.accepted.append,
                clock=clock,
                source_id=descriptor.source_id,
            )
        elif sink is not None:
            raise CollectorError("pass either a relay or a sink, not both")
        self._relay = relay
        self._normalizer = (
            normalizer if normalizer is not None else _normalizer_for(source_id, descriptor)
        )
        self._submitter: Submitter = (
            submitter if submitter is not None else self._relay.submit
        )
        self._cursor = 0

    @property
    def relay(self) -> Relay:
        return self._relay

    @property
    def source_id(self) -> str:
        return self._descriptor.source_id

    @property
    def cursor(self) -> int:
        return self._cursor

    def collect(self) -> list[dict[str, Any]]:
        """Run one pass and return the relay's per-record results.

        Preparation is all-or-nothing: every row is normalized and validated
        before the first submission, so a rejected record leaves the relay's
        cursor and idempotency map untouched.
        """

        rows = self._fetch()
        prepared: list[tuple[dict[str, Any], str, int]] = []
        cursor = self._cursor
        try:
            for row in rows:
                if not isinstance(row, Mapping):
                    raise CollectorError("proxy record is not a JSON object")
                cursor += 1
                body, key = self._prepare(row, cursor)
                prepared.append((body, key, cursor))
        except CollectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken stream fails closed
            raise CollectorError(
                f"record stream failed: {type(exc).__name__}"
            ) from exc

        results: list[dict[str, Any]] = []
        for body, key, record_cursor in prepared:
            payload = canonical_json(body)
            headers = {
                IDEMPOTENCY_HEADER: key,
                SIGNATURE_HEADER: f"sha256={self._relay.sign(payload, key)}",
            }
            result = self._submitter(headers, payload, record_cursor)
            self._cursor = record_cursor
            results.append(dict(result))
        return results

    def _fetch(self) -> Iterable[Mapping[str, Any]]:
        try:
            rows = self._fetcher(self._proxy_url)
        except Exception as exc:  # noqa: BLE001 - no fetch means no submission
            raise CollectorError(f"fetch failed: {type(exc).__name__}") from exc
        if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Iterable):
            raise CollectorError("fetcher must return an iterable of records")
        return rows

    def _prepare(
        self, row: Mapping[str, Any], cursor: int
    ) -> tuple[dict[str, Any], str]:
        if self._normalizer is not None:
            record = self._normalizer(row, cursor=cursor)
            if not isinstance(record, NormalizedRecord):
                raise CollectorError("normalizer did not return a NormalizedRecord")
            if record.source_id != self._descriptor.source_id:
                raise CollectorError(
                    f"normalizer produced source {record.source_id!r}, "
                    f"schema declares {self._descriptor.source_id!r}"
                )
            body = record.as_relay_record()
            stable_id = record.stable_id
            revision = record.revision
        else:
            body = dict(row)
            stable_id = self._stable_id(body)
            revision = self._revision(body)

        self._descriptor.validate_record(body)
        return body, self._idempotency_key(stable_id, revision)

    def _stable_id(self, record: Mapping[str, Any]) -> str:
        parts: list[str] = []
        for name in self._descriptor.stable_id_fields:
            value = record.get(name)
            if not isinstance(value, str) or not value.strip():
                raise CollectorError(f"record has no stable id in {name!r}")
            parts.append(value)
        return "|".join(parts)

    def _revision(self, record: Mapping[str, Any]) -> str:
        value = record.get(self._descriptor.revision_field)
        if not isinstance(value, str) or not value.strip():
            raise CollectorError(
                f"record has no revision in {self._descriptor.revision_field!r}"
            )
        return value

    def _idempotency_key(self, stable_id: str, revision: str) -> str:
        key = f"{self._descriptor.source_id}:{stable_id}:{revision}"
        if len(key) > _MAX_IDEMPOTENCY_KEY:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            key = f"{self._descriptor.source_id}:{digest}"
        return key


class HttpRelaySubmitter:
    """Submit a signed body to a relay listener over HTTP.

    The collector pod and the relay listener are the same process; this adapter
    lets the collector speak to the listener exactly as a separate collector
    would, without the library taking on a socket dependency for tests.
    """

    def __init__(
        self, url: str, *, timeout: float = _FETCH_TIMEOUT_SECONDS
    ) -> None:
        self._url = _check_http_url(url, "relay URL")
        self._timeout = timeout

    def submit(
        self, headers: Mapping[str, str], body: bytes, cursor: int
    ) -> Mapping[str, Any]:
        separator = "&" if "?" in self._url else "?"
        target = f"{self._url}{separator}cursor={cursor}"
        request = urllib.request.Request(
            target,
            data=body,
            method="POST",
            headers={**{str(k): str(v) for k, v in headers.items()},
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            raw = response.read()
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CollectorError("relay response is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise CollectorError("relay response must be a JSON object")
        return value
