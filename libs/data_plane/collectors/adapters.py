"""Normalize one provider-shaped export row into the portable record shape.

An adapter is a pure function over an already-fetched dictionary: it never
contacts a provider, never invents an identity, and never silently drops a
provider field. A missing stable identity or revision is refused because such a
record cannot be deduplicated or ordered. Every gap the provider imposes is
returned as a named :class:`FidelityLimit` beside the record.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from email import message_from_string
from types import MappingProxyType
from typing import Any, Mapping

from ..errors import ValidationError
from ..model import FidelityLimit
from .schemas import descriptor_for_source

MAIL_SOURCE = "tuta.mail"
FINANCE_SOURCE = "finance.ledger"
CALENDAR_SOURCE = "calendar.events"
PHONE_SOURCE = "phone.calls"
BRAVE_SOURCE = "brave.history"
IMMICH_SOURCE = "immich.photos"

_MISSING = object()


def _limits(source_id: str) -> tuple[FidelityLimit, ...]:
    return descriptor_for_source(source_id).fidelity_limits


MAIL_UNSUPPORTED = _limits(MAIL_SOURCE)
FINANCE_UNSUPPORTED = _limits(FINANCE_SOURCE)
CALENDAR_UNSUPPORTED = _limits(CALENDAR_SOURCE)
PHONE_UNSUPPORTED = _limits(PHONE_SOURCE)
BRAVE_UNSUPPORTED = _limits(BRAVE_SOURCE)
IMMICH_UNSUPPORTED = _limits(IMMICH_SOURCE)


@dataclass(frozen=True)
class Attachment:
    """One preserved attachment: exact bytes, their hash, and their media type."""

    filename: str
    media_type: str
    sha256: str
    content_base64: str

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str):
            raise ValidationError("attachment filename must be a string")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValidationError("attachment media type must be non-empty")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValidationError(f"attachment hash {self.sha256!r} is not a SHA-256")
        try:
            data = base64.b64decode(self.content_base64, validate=True)
        except (ValueError, TypeError) as error:
            raise ValidationError("attachment content is not base64") from error
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValidationError("attachment hash does not match its content")

    def as_dict(self) -> dict[str, str]:
        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "content_base64": self.content_base64,
        }


@dataclass(frozen=True)
class NormalizedRecord:
    """One provider row after normalization, with its fidelity gaps named."""

    source_id: str
    stable_id: str
    revision: str
    tombstone: bool
    cursor: int | None
    fields: Mapping[str, Any]
    attachments: tuple[Attachment, ...] = ()
    unsupported_fields: tuple[FidelityLimit, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValidationError("normalized record source id must be non-empty")
        if not isinstance(self.stable_id, str) or not self.stable_id.strip():
            raise ValidationError("normalized record stable id must be non-empty")
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValidationError("normalized record revision must be non-empty")
        if not isinstance(self.tombstone, bool):
            raise ValidationError("normalized record tombstone must be a bool")
        if self.cursor is not None and (
            isinstance(self.cursor, bool)
            or not isinstance(self.cursor, int)
            or self.cursor < 1
        ):
            raise ValidationError("normalized record cursor must be a positive integer")
        if not isinstance(self.fields, Mapping):
            raise ValidationError("normalized record fields must be a mapping")
        collisions = {"source_id", "tombstone", "cursor"} & set(self.fields)
        if collisions:
            raise ValidationError(
                f"normalized record fields collide with metadata {sorted(collisions)!r}"
            )
        for name in self.fields:
            if not isinstance(name, str) or not name.strip():
                raise ValidationError("normalized record field names must be non-empty")

        attachments = tuple(self.attachments)
        for attachment in attachments:
            if not isinstance(attachment, Attachment):
                raise ValidationError("normalized record attachments must be Attachment")
        unsupported = tuple(self.unsupported_fields)
        for limit in unsupported:
            if not isinstance(limit, FidelityLimit):
                raise ValidationError(
                    "normalized record unsupported fields must be FidelityLimit"
                )
            limit.validate()
        names = [limit.field for limit in unsupported]
        if len(names) != len(set(names)):
            raise ValidationError("normalized record repeats an unsupported field")

        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "attachments", attachments)
        object.__setattr__(self, "unsupported_fields", unsupported)

    @property
    def unsupported_names(self) -> tuple[str, ...]:
        return tuple(limit.field for limit in self.unsupported_fields)

    def as_relay_record(self) -> dict[str, Any]:
        """The flattened record a relay schema validates."""

        record: dict[str, Any] = {
            "source_id": self.source_id,
            "tombstone": self.tombstone,
        }
        record.update(_json_value(self.fields))
        return record

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "stable_id": self.stable_id,
            "revision": self.revision,
            "tombstone": self.tombstone,
            "cursor": self.cursor,
            "fields": _json_value(self.fields),
            "attachments": [item.as_dict() for item in self.attachments],
            "unsupported_fields": [
                {"field": limit.field, "reason": limit.reason}
                for limit in self.unsupported_fields
            ],
        }


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _at(
    record: Mapping[str, Any], *names: str, default: Any = _MISSING
) -> Any:
    if not isinstance(record, Mapping):
        raise ValidationError("provider record must be a mapping")
    for name in names:
        if name in record:
            return record[name]
    if default is not _MISSING:
        return default
    raise ValidationError(f"provider record is missing {names[0]!r}")


def _optional_text(record: Mapping[str, Any], *names: str) -> str:
    value = _at(record, *names, default=None)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"provider field {names[0]!r} must be a string")
    return value


def _identity(record: Mapping[str, Any], *names: str) -> str:
    value = _at(record, *names, default=None)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"provider record has no stable id in {names[0]!r}")
    return value


def _revision(record: Mapping[str, Any], *names: str) -> str:
    value = _at(record, *names, default=None)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(f"provider record has no revision in {names[0]!r}")
    revision = str(value)
    if not revision.strip():
        raise ValidationError(f"provider record has no revision in {names[0]!r}")
    return revision


def _tombstone(record: Mapping[str, Any], *flags: str) -> bool:
    marker = record.get("tombstone")
    if marker is not None:
        if not isinstance(marker, bool):
            raise ValidationError("provider tombstone flag must be a bool")
        if marker:
            return True
    return any(bool(record.get(flag)) for flag in ("deleted", "expired") + flags)


def _bool(record: Mapping[str, Any], *names: str, default: bool = False) -> bool:
    value = _at(record, *names, default=default)
    if not isinstance(value, bool):
        raise ValidationError(f"provider field {names[0]!r} must be a bool")
    return value


def _integer(
    record: Mapping[str, Any], *names: str, default: int | None = None
) -> int:
    value = _at(record, *names, default=default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"provider field {names[0]!r} must be an integer")
    return value


def _strings(record: Mapping[str, Any], *names: str) -> list[str]:
    value = _at(record, *names, default=None)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"provider field {names[0]!r} must be a sequence")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(f"provider field {names[0]!r} must contain strings")
        items.append(item)
    return items


def _event_time(record: Mapping[str, Any], *names: str) -> str:
    value = _at(record, *names, default=None)
    if isinstance(value, Mapping):
        value = value.get("dateTime") or value.get("date")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"provider field {names[0]!r} must be a timestamp")
    return value


def _attachment(item: Mapping[str, Any]) -> Attachment:
    if not isinstance(item, Mapping):
        raise ValidationError("provider attachment must be a mapping")
    encoded = _at(
        item, "dataBase64", "data_base64", "contentBase64", default=None
    )
    if not isinstance(encoded, str):
        raise ValidationError("provider attachment has no base64 content")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise ValidationError("provider attachment is not base64") from error
    return Attachment(
        filename=_optional_text(item, "filename", "name"),
        media_type=_optional_text(item, "mimeType", "media_type", "contentType")
        or "application/octet-stream",
        sha256=hashlib.sha256(data).hexdigest(),
        content_base64=base64.b64encode(data).decode("ascii"),
    )


def _attachments(record: Mapping[str, Any]) -> tuple[Attachment, ...]:
    raw = _at(record, "attachments", default=None)
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("provider attachments must be a sequence")
    return tuple(_attachment(item) for item in raw)


def _mime_body_text(raw_mime: str) -> str:
    try:
        message = message_from_string(raw_mime)
    except Exception:
        return ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        return ""
    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(
            message.get_content_charset() or "utf-8", errors="replace"
        )
    if isinstance(payload, str):
        return payload
    return ""


def _cursor(record: Mapping[str, Any], cursor: int | None) -> int | None:
    return cursor if cursor is not None else record.get("cursor")


def _mime_attachments(raw_mime: str) -> tuple[Attachment, ...]:
    """Named MIME parts, so a provider that only exports raw MIME still hashes attachments."""

    try:
        message = message_from_string(raw_mime)
    except Exception:
        return ()
    if not message.is_multipart():
        return ()
    attachments: list[Attachment] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if not filename or not isinstance(payload, bytes):
            continue
        attachments.append(
            Attachment(
                filename=filename,
                media_type=part.get_content_type(),
                sha256=hashlib.sha256(payload).hexdigest(),
                content_base64=base64.b64encode(payload).decode("ascii"),
            )
        )
    return tuple(attachments)


def normalize_mail(
    record: Mapping[str, Any], *, cursor: int | None = None
) -> NormalizedRecord:
    """Preserve the full MIME source and every attachment byte-for-byte."""

    stable_id = _identity(record, "messageId", "message_id", "id")
    revision = _revision(record, "revision")
    tombstone = _tombstone(record, "trashed", "deletedAt", "deleted_at")
    fields: dict[str, Any] = {
        "message_id": stable_id,
        "revision": revision,
    }
    attachments: tuple[Attachment, ...] = ()
    if not tombstone:
        fields.update(
            {
                "subject": _optional_text(record, "subject"),
                "sender": _optional_text(record, "sender", "from"),
                "recipients": _strings(record, "recipients", "to"),
                "received_at": _optional_text(record, "receivedAt", "received_at"),
                "labels": _strings(record, "labels"),
            }
        )
        raw_mime = _optional_text(record, "rawMime", "raw_mime", "mime")
        if raw_mime:
            fields["mime"] = raw_mime
            body = _mime_body_text(raw_mime)
            if body:
                fields["body_text"] = body
        attachments = _attachments(record)
        if raw_mime:
            known = {(item.filename, item.sha256) for item in attachments}
            attachments += tuple(
                item
                for item in _mime_attachments(raw_mime)
                if (item.filename, item.sha256) not in known
            )
        if attachments:
            fields["attachment_sha256"] = [item.sha256 for item in attachments]
            fields["attachment_filenames"] = [item.filename for item in attachments]

    return NormalizedRecord(
        source_id=MAIL_SOURCE,
        stable_id=stable_id,
        revision=revision,
        tombstone=tombstone,
        cursor=_cursor(record, cursor),
        fields=fields,
        attachments=attachments,
        unsupported_fields=MAIL_UNSUPPORTED,
    )


def normalize_finance(
    record: Mapping[str, Any], *, cursor: int | None = None
) -> NormalizedRecord:
    """Keep amounts in minor units; the original statement is provider-only."""

    stable_id = _identity(record, "transactionId", "transaction_id", "id")
    revision = _revision(record, "revision")
    tombstone = _tombstone(record, "voided", "deletedAt", "deleted_at")
    fields: dict[str, Any] = {
        "transaction_id": stable_id,
        "revision": revision,
    }
    if not tombstone:
        amount = _at(record, "amount", default=None)
        if isinstance(amount, Mapping):
            minor = amount.get("minor", amount.get("amount_minor"))
            currency = amount.get("currency")
        else:
            minor = record.get("amount_minor")
            currency = record.get("currency")
        if isinstance(minor, bool) or not isinstance(minor, int):
            raise ValidationError("finance record has no integer amount_minor")
        if not isinstance(currency, str) or not currency.strip():
            raise ValidationError("finance record has no currency")
        fields.update(
            {
                "account_alias": _optional_text(record, "accountAlias", "account_alias", "account"),
                "amount_minor": minor,
                "currency": currency,
                "posted_at": _optional_text(record, "postedAt", "posted_at"),
                "description": _optional_text(record, "description"),
                "merchant": _optional_text(record, "merchant"),
                "pending": _bool(record, "pending"),
            }
        )

    return NormalizedRecord(
        source_id=FINANCE_SOURCE,
        stable_id=stable_id,
        revision=revision,
        tombstone=tombstone,
        cursor=_cursor(record, cursor),
        fields=fields,
        unsupported_fields=FINANCE_UNSUPPORTED,
    )


def normalize_calendar(
    record: Mapping[str, Any], *, cursor: int | None = None
) -> NormalizedRecord:
    """Keep the master event; per-instance overrides stay provider-local."""

    stable_id = _identity(record, "eventId", "event_id", "id")
    revision = _revision(record, "revision")
    tombstone = _tombstone(record, "cancelled", "canceled", "deletedAt")
    fields: dict[str, Any] = {
        "event_id": stable_id,
        "revision": revision,
    }
    if not tombstone:
        recurrence = _at(record, "recurrence", default=None)
        if isinstance(recurrence, (list, tuple)):
            rule = ";".join(str(item) for item in recurrence)
        elif isinstance(recurrence, str):
            rule = recurrence
        else:
            rule = ""
        attendees: list[str] = []
        raw_attendees = _at(record, "attendees", default=None)
        if isinstance(raw_attendees, (list, tuple)):
            for item in raw_attendees:
                if isinstance(item, str):
                    attendees.append(item)
                elif isinstance(item, Mapping):
                    address = item.get("email") or item.get("address")
                    if isinstance(address, str):
                        attendees.append(address)
                else:
                    raise ValidationError("calendar attendee must be a string or mapping")
        elif raw_attendees is not None:
            raise ValidationError("calendar attendees must be a sequence")
        fields.update(
            {
                "uid": _optional_text(record, "uid", "iCalUID"),
                "summary": _optional_text(record, "summary"),
                "description": _optional_text(record, "description"),
                "location": _optional_text(record, "location"),
                "starts_at": _event_time(record, "start", "startsAt", "starts_at"),
                "ends_at": _event_time(record, "end", "endsAt", "ends_at"),
                "all_day": _bool(record, "allDay", "all_day"),
                "recurrence_rule": rule,
                "attendees": attendees,
                "status": _optional_text(record, "status"),
                "updated_at": _optional_text(record, "updatedAt", "updated_at"),
            }
        )

    return NormalizedRecord(
        source_id=CALENDAR_SOURCE,
        stable_id=stable_id,
        revision=revision,
        tombstone=tombstone,
        cursor=_cursor(record, cursor),
        fields=fields,
        unsupported_fields=CALENDAR_UNSUPPORTED,
    )


def normalize_phone(
    record: Mapping[str, Any], *, cursor: int | None = None
) -> NormalizedRecord:
    """Keep the call log row and transcript; carrier audio stays with the carrier."""

    stable_id = _identity(record, "callId", "call_id", "id")
    revision = _revision(record, "revision")
    tombstone = _tombstone(record, "deletedAt", "deleted_at")
    fields: dict[str, Any] = {
        "call_id": stable_id,
        "revision": revision,
    }
    if not tombstone:
        direction = _optional_text(record, "direction")
        if direction and direction not in {"incoming", "outgoing", "missed"}:
            raise ValidationError(f"phone direction {direction!r} is not supported")
        contact = _at(record, "contact", default=None)
        contact_alias = (
            contact.get("displayName") or contact.get("display_name")
            if isinstance(contact, Mapping)
            else None
        )
        voicemail = _at(record, "voicemail", default=None)
        transcript = (
            voicemail.get("transcript")
            if isinstance(voicemail, Mapping)
            else None
        )
        fields.update(
            {
                "direction": direction,
                "started_at": _optional_text(record, "startedAt", "started_at"),
                "duration_seconds": _integer(
                    record, "durationSeconds", "duration_seconds", default=0
                ),
                "contact_alias": contact_alias
                if isinstance(contact_alias, str)
                else _optional_text(record, "contactAlias", "contact_alias"),
                "missed": _bool(record, "missed", default=direction == "missed"),
                "voicemail_transcript": transcript
                if isinstance(transcript, str)
                else _optional_text(record, "voicemailTranscript", "voicemail_transcript"),
                "labels": _strings(record, "labels"),
            }
        )

    return NormalizedRecord(
        source_id=PHONE_SOURCE,
        stable_id=stable_id,
        revision=revision,
        tombstone=tombstone,
        cursor=_cursor(record, cursor),
        fields=fields,
        unsupported_fields=PHONE_UNSUPPORTED,
    )


def normalize_brave(
    record: Mapping[str, Any], *, cursor: int | None = None
) -> NormalizedRecord:
    """Keep the visit row; favicons are derived and rebuilt from the URL."""

    stable_id = _identity(record, "entryId", "entry_id", "id")
    revision = _revision(record, "revision")
    tombstone = _tombstone(record, "deletedAt", "deleted_at")
    fields: dict[str, Any] = {
        "entry_id": stable_id,
        "revision": revision,
    }
    if not tombstone:
        fields.update(
            {
                "url": _optional_text(record, "url"),
                "title": _optional_text(record, "title"),
                "visited_at": _optional_text(record, "visitedAt", "visited_at"),
                "transition": _optional_text(record, "transition"),
            }
        )

    return NormalizedRecord(
        source_id=BRAVE_SOURCE,
        stable_id=stable_id,
        revision=revision,
        tombstone=tombstone,
        cursor=_cursor(record, cursor),
        fields=fields,
        unsupported_fields=BRAVE_UNSUPPORTED,
    )


def normalize_immich(
    record: Mapping[str, Any], *, cursor: int | None = None
) -> NormalizedRecord:
    """Keep the original asset and its checksum; derived renders are dropped."""

    stable_id = _identity(record, "assetId", "asset_id", "id")
    revision = _revision(record, "revision")
    tombstone = _tombstone(record, "trashed", "deletedAt", "deleted_at")
    fields: dict[str, Any] = {
        "asset_id": stable_id,
        "revision": revision,
    }
    attachments: tuple[Attachment, ...] = ()
    if not tombstone:
        original = _at(record, "original", "file", default=None)
        declared = _optional_text(record, "checksum")
        if original is not None:
            if not isinstance(original, Mapping):
                raise ValidationError("immich original asset must be a mapping")
            attachment = _attachment(original)
            if declared and declared != attachment.sha256:
                raise ValidationError(
                    "immich checksum does not match the preserved original"
                )
            checksum = attachment.sha256
            attachments = (attachment,)
        elif declared:
            checksum = declared
        else:
            raise ValidationError("immich record has no asset checksum")
        fields.update(
            {
                "captured_at": _optional_text(record, "capturedAt", "captured_at"),
                "checksum": checksum,
                "album_ids": _strings(record, "albumIds", "album_ids"),
                "original_filename": _optional_text(record, "originalFilename", "original_filename")
                or (attachments[0].filename if attachments else ""),
                "media_type": _optional_text(record, "mediaType", "media_type")
                or (attachments[0].media_type if attachments else ""),
            }
        )

    return NormalizedRecord(
        source_id=IMMICH_SOURCE,
        stable_id=stable_id,
        revision=revision,
        tombstone=tombstone,
        cursor=_cursor(record, cursor),
        fields=fields,
        attachments=attachments,
        unsupported_fields=IMMICH_UNSUPPORTED,
    )


NORMALIZERS = {
    "mail": normalize_mail,
    "finance": normalize_finance,
    "calendar": normalize_calendar,
    "phone": normalize_phone,
    "brave": normalize_brave,
    "immich": normalize_immich,
}
