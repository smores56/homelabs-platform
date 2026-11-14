"""Collector schema descriptors and provider adapters, exercised offline."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from pathlib import Path

from data_plane.collectors import schemas
from data_plane.collectors.adapters import (
    NORMALIZERS,
    Attachment,
    NormalizedRecord,
    normalize_finance,
    normalize_immich,
    normalize_mail,
)
from data_plane.collectors.fidelity import FidelityDeclaration
from data_plane.collectors.relay import RecordSchema
from data_plane.errors import ValidationError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROVIDERS = FIXTURES / "providers"

ADAPTERS = ("mail", "finance", "calendar", "phone", "brave", "immich")
EXPECTED_SOURCES = {
    "mail": "tuta.mail",
    "finance": "finance.ledger",
    "calendar": "calendar.events",
    "phone": "phone.calls",
    "brave": "brave.history",
    "immich": "immich.photos",
    "photos": "photos.library",
    "music": "music.library",
    "books": "books.opds",
    "notes": "notes.markdown",
}
TOMBSTONE_FLAGS = {
    "mail": "deleted",
    "finance": "voided",
    "calendar": "cancelled",
    "phone": "deleted",
    "brave": "deleted",
    "immich": "deleted",
}
STABLE_ID_KEYS = {
    "mail": "messageId",
    "finance": "transactionId",
    "calendar": "eventId",
    "phone": "callId",
    "brave": "entryId",
    "immich": "assetId",
}


def provider(name: str) -> dict:
    return json.loads((PROVIDERS / f"{name}.json").read_text(encoding="utf-8"))


class SchemaDescriptorTests(unittest.TestCase):
    def test_every_descriptor_loads_as_runtime_contract_types(self) -> None:
        self.assertEqual(
            {name: schemas.load_descriptor(name).source_id for name in schemas.schema_names()},
            EXPECTED_SOURCES,
        )
        for name in schemas.schema_names():
            with self.subTest(name=name):
                descriptor = schemas.load_descriptor(name)
                descriptor.validate()
                schema = descriptor.relay_schema()
                declaration = descriptor.fidelity_declaration()
                self.assertIsInstance(schema, RecordSchema)
                self.assertIsInstance(declaration, FidelityDeclaration)
                declaration.validate()
                self.assertEqual(declaration.source_id, descriptor.source_id)
                self.assertEqual(declaration.preserved_fields, descriptor.preserved_fields)
                self.assertEqual(
                    declaration.unsupported_fields, descriptor.fidelity_limits
                )

    def test_every_descriptor_declares_identity_revision_and_limits(self) -> None:
        for name in schemas.schema_names():
            with self.subTest(name=name):
                descriptor = schemas.load_descriptor(name)
                self.assertGreaterEqual(descriptor.max_bytes, 1)
                self.assertGreaterEqual(descriptor.max_records_per_second, 1)
                self.assertTrue(descriptor.title)
                self.assertIn("source_id", descriptor.required_fields)
                self.assertIn("tombstone", descriptor.required_fields)
                self.assertIn(descriptor.tombstone_field, descriptor.required_fields)
                self.assertIn(descriptor.revision_field, descriptor.required_fields)
                self.assertTrue(descriptor.stable_id_fields)
                for field in descriptor.stable_id_fields:
                    self.assertIn(field, descriptor.required_fields)
                for field in descriptor.attachment_hash_fields:
                    self.assertIn(field, descriptor.properties)
                for limit in descriptor.fidelity_limits:
                    self.assertTrue(limit.reason)
                    self.assertNotIn(limit.field, descriptor.properties)
                for python_type in descriptor.field_types().values():
                    self.assertIn(
                        python_type, (str, int, float, bool, list, dict)
                    )

    def test_relay_schema_rejects_an_empty_record(self) -> None:
        for name in schemas.schema_names():
            with self.subTest(name=name):
                schema = schemas.load_descriptor(name).relay_schema()
                with self.assertRaises(ValidationError):
                    schema.validate({})

    def test_identity_only_tombstone_validates(self) -> None:
        for name in schemas.schema_names():
            with self.subTest(name=name):
                descriptor = schemas.load_descriptor(name)
                record = {
                    "source_id": descriptor.source_id,
                    "tombstone": True,
                    descriptor.stable_id_fields[0]: "synthetic-id",
                    descriptor.revision_field: "1",
                }
                descriptor.validate_record(record)

    def test_missing_identity_or_revision_is_refused(self) -> None:
        for name in schemas.schema_names():
            with self.subTest(name=name):
                descriptor = schemas.load_descriptor(name)
                stable = descriptor.stable_id_fields[0]
                record = {
                    "source_id": descriptor.source_id,
                    "tombstone": False,
                    stable: "synthetic-id",
                    descriptor.revision_field: "1",
                }
                missing_id = dict(record)
                missing_id.pop(stable)
                missing_revision = dict(record)
                missing_revision.pop(descriptor.revision_field)
                with self.assertRaises(ValidationError):
                    descriptor.validate_record(missing_id)
                with self.assertRaises(ValidationError):
                    descriptor.validate_record(missing_revision)

    def test_unknown_descriptor_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            schemas.load_descriptor("../mail")
        with self.assertRaises(ValidationError):
            schemas.descriptor_for_source("example.unpublished")


class AdapterTests(unittest.TestCase):
    def normalized(self, name: str) -> NormalizedRecord:
        return NORMALIZERS[name](provider(name))

    def test_every_adapter_output_matches_its_descriptor(self) -> None:
        for name in ADAPTERS:
            with self.subTest(name=name):
                record = self.normalized(name)
                descriptor = schemas.descriptor_for_source(record.source_id)
                relay_record = record.as_relay_record()
                descriptor.validate_record(relay_record)
                self.assertLessEqual(set(relay_record), set(descriptor.properties))
                self.assertFalse(record.tombstone)
                self.assertIsNone(record.cursor)
                self.assertEqual(
                    record.stable_id, relay_record[descriptor.stable_id_fields[0]]
                )
                self.assertEqual(
                    record.revision, relay_record[descriptor.revision_field]
                )
                self.assertEqual(
                    set(record.unsupported_names),
                    {limit.field for limit in descriptor.fidelity_limits},
                )

    def test_mail_preserves_full_mime_and_attachment_bytes(self) -> None:
        fixture = provider("mail")
        record = normalize_mail(fixture)

        self.assertEqual(record.fields["mime"], fixture["rawMime"])
        self.assertIn("Synthetic body text.", record.fields["body_text"])
        self.assertEqual(record.fields["recipients"], ["recipient@example.invalid"])
        self.assertEqual(len(record.attachments), 1)
        attachment = record.attachments[0]
        data = base64.b64decode(fixture["attachments"][0]["dataBase64"])
        self.assertEqual(attachment.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(
            attachment.content_base64, base64.b64encode(data).decode("ascii")
        )
        self.assertEqual(record.fields["attachment_sha256"], [attachment.sha256])
        self.assertEqual(record.fields["attachment_filenames"], ["synthetic.txt"])

    def test_finance_keeps_integer_minor_units_and_currency(self) -> None:
        record = normalize_finance(provider("finance"))

        self.assertEqual(record.fields["amount_minor"], -1250)
        self.assertIsInstance(record.fields["amount_minor"], int)
        self.assertEqual(record.fields["currency"], "USD")
        self.assertEqual(record.attachments, ())
        self.assertIn("statement_pdf", record.unsupported_names)

    def test_calendar_flattens_nested_times_and_attendees(self) -> None:
        record = self.normalized("calendar")

        self.assertEqual(record.fields["starts_at"], "2026-05-01T09:00:00+00:00")
        self.assertEqual(record.fields["ends_at"], "2026-05-01T10:00:00+00:00")
        self.assertEqual(record.fields["attendees"], ["attendee@example.invalid"])
        self.assertEqual(record.fields["recurrence_rule"], "RRULE:FREQ=WEEKLY")
        self.assertIn("recurrence_exceptions", record.unsupported_names)

    def test_phone_keeps_transcript_but_not_carrier_audio(self) -> None:
        record = self.normalized("phone")

        self.assertEqual(
            record.fields["voicemail_transcript"], "Synthetic voicemail transcript."
        )
        self.assertEqual(record.attachments, ())
        self.assertEqual(record.fields["contact_alias"], "Example Contact")
        self.assertIn("carrier_voicemail_audio", record.unsupported_names)

    def test_brave_keeps_the_visit_row_and_drops_favicons(self) -> None:
        record = self.normalized("brave")

        self.assertEqual(record.fields["url"], "https://example.invalid/synthetic")
        self.assertEqual(record.attachments, ())
        self.assertEqual(record.unsupported_names, ("server_side_favicons",))

    def test_immich_preserves_the_original_and_verifies_its_checksum(self) -> None:
        fixture = provider("immich")
        record = normalize_immich(fixture)

        self.assertEqual(len(record.attachments), 1)
        attachment = record.attachments[0]
        self.assertEqual(record.fields["checksum"], attachment.sha256)
        self.assertEqual(record.fields["checksum"], fixture["checksum"])
        self.assertEqual(record.fields["original_filename"], "synthetic.png")
        self.assertIn("face_embeddings", record.unsupported_names)

        tampered = dict(fixture)
        tampered["checksum"] = "0" * 64
        with self.assertRaises(ValidationError):
            normalize_immich(tampered)

    def test_mail_extracts_attachments_from_raw_mime_when_not_listed(self) -> None:
        payload = base64.b64encode(b"synthetic mime part").decode("ascii")
        fixture = {
            "messageId": "synthetic-msg-2",
            "revision": "1",
            "rawMime": (
                "From: sender@example.invalid\r\n"
                "To: recipient@example.invalid\r\n"
                "MIME-Version: 1.0\r\n"
                "Content-Type: multipart/mixed; boundary=example\r\n"
                "\r\n"
                "--example\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "\r\n"
                "Synthetic body.\r\n"
                "--example\r\n"
                "Content-Type: application/octet-stream\r\n"
                "Content-Disposition: attachment; filename=synthetic.bin\r\n"
                "Content-Transfer-Encoding: base64\r\n"
                "\r\n"
                f"{payload}\r\n"
                "--example--\r\n"
            ),
        }
        record = normalize_mail(fixture)

        self.assertEqual(len(record.attachments), 1)
        self.assertEqual(record.attachments[0].filename, "synthetic.bin")
        self.assertEqual(
            record.attachments[0].sha256,
            hashlib.sha256(b"synthetic mime part").hexdigest(),
        )
        self.assertEqual(record.fields["attachment_sha256"], [record.attachments[0].sha256])

    def test_tombstones_drop_content_but_keep_identity(self) -> None:
        for name in ADAPTERS:
            with self.subTest(name=name):
                fixture = provider(name)
                fixture[TOMBSTONE_FLAGS[name]] = True
                record = NORMALIZERS[name](fixture)
                descriptor = schemas.descriptor_for_source(record.source_id)

                self.assertTrue(record.tombstone)
                self.assertEqual(record.attachments, ())
                descriptor.validate_record(record.as_relay_record())
                self.assertEqual(
                    set(record.fields),
                    {descriptor.stable_id_fields[0], descriptor.revision_field},
                )

    def test_missing_stable_id_is_refused(self) -> None:
        for name in ADAPTERS:
            with self.subTest(name=name):
                fixture = provider(name)
                fixture.pop(STABLE_ID_KEYS[name])
                with self.assertRaises(ValidationError):
                    NORMALIZERS[name](fixture)

    def test_missing_or_empty_revision_is_refused(self) -> None:
        for name in ADAPTERS:
            with self.subTest(name=name):
                missing = provider(name)
                missing.pop("revision")
                with self.assertRaises(ValidationError):
                    NORMALIZERS[name](missing)

                empty = provider(name)
                empty["revision"] = "  "
                with self.assertRaises(ValidationError):
                    NORMALIZERS[name](empty)

    def test_unsupported_fields_never_overlap_preserved_fields(self) -> None:
        for name in ADAPTERS:
            with self.subTest(name=name):
                record = self.normalized(name)
                self.assertFalse(
                    set(record.unsupported_names) & set(record.fields)
                )

    def test_normalized_record_refuses_bad_metadata(self) -> None:
        fields = {"synthetic_id": "1", "revision": "1"}
        with self.assertRaises(ValidationError):
            NormalizedRecord(
                source_id="example.source",
                stable_id="",
                revision="1",
                tombstone=False,
                cursor=None,
                fields=fields,
            )
        with self.assertRaises(ValidationError):
            NormalizedRecord(
                source_id="example.source",
                stable_id="1",
                revision="1",
                tombstone=False,
                cursor=0,
                fields=fields,
            )
        with self.assertRaises(ValidationError):
            NormalizedRecord(
                source_id="example.source",
                stable_id="1",
                revision="1",
                tombstone=False,
                cursor=None,
                fields={"source_id": "collides"},
            )

    def test_attachment_hash_must_match_its_bytes(self) -> None:
        data = b"synthetic"
        with self.assertRaises(ValidationError):
            Attachment(
                filename="synthetic.txt",
                media_type="text/plain",
                sha256="0" * 64,
                content_base64=base64.b64encode(data).decode("ascii"),
            )

    def test_cursor_is_carried_when_supplied(self) -> None:
        record = NORMALIZERS["brave"](provider("brave"), cursor=12)
        self.assertEqual(record.cursor, 12)


if __name__ == "__main__":
    unittest.main()
