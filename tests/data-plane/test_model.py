"""Contract validation tests: authority is per field and safety is explicit."""

from __future__ import annotations

import unittest

from data_plane.errors import ContractError
from data_plane.model import (
    Authority,
    Capability,
    DatasetMount,
    EffectClass,
    FieldAuthority,
    RecoveryGroup,
    SourceContract,
    Visibility,
    validate_many,
    write_capabilities,
)


def read(name: str = "read") -> Capability:
    return Capability(name=name, effect=EffectClass.READ)


def create() -> Capability:
    return Capability(name="create", effect=EffectClass.CREATE, reversible=True)


def update() -> Capability:
    return Capability(name="update", effect=EffectClass.UPDATE, reversible=True)


def delete() -> Capability:
    return Capability(
        name="delete",
        effect=EffectClass.DELETE,
        reversible=False,
        requires_confirmation=True,
    )


def contract(**overrides) -> SourceContract:
    values = dict(
        source_id="books",
        visibility=Visibility.GENERAL,
        version=1,
        recovery_group="books",
        stable_id_fields=("id",),
        capabilities=(read(), create(), update(), delete()),
        authority=(FieldAuthority(field="title", authority=Authority.PORTABLE_FILE),),
        revision_field="rev",
        tombstone_field="deleted_at",
        attachment_hash_fields=("sha256",),
    )
    values.update(overrides)
    return SourceContract(**values)


class CapabilityTests(unittest.TestCase):
    def test_destructive_effect_must_require_confirmation(self):
        with self.assertRaises(ContractError):
            Capability(name="delete", effect=EffectClass.DELETE, reversible=False).validate()

    def test_bulk_and_reclassify_require_confirmation(self):
        for effect in (EffectClass.BULK, EffectClass.RECLASSIFY):
            with self.subTest(effect=effect):
                with self.assertRaises(ContractError):
                    Capability(name=effect.value, effect=effect).validate()


class SourceContractTests(unittest.TestCase):
    def test_valid_contract_passes(self):
        contract().validate()

    def test_duplicate_capability_rejected(self):
        with self.assertRaises(ContractError):
            contract(capabilities=(read(), read("read"))).validate()

    def test_update_without_revision_rejected(self):
        with self.assertRaises(ContractError):
            contract(revision_field=None).validate()

    def test_delete_without_tombstone_rejected(self):
        with self.assertRaises(ContractError):
            contract(tombstone_field=None).validate()

    def test_no_capabilities_rejected(self):
        with self.assertRaises(ContractError):
            contract(capabilities=()).validate()

    def test_duplicate_field_authority_rejected(self):
        item = FieldAuthority(field="title", authority=Authority.PROVIDER)
        with self.assertRaises(ContractError):
            contract(authority=(item, item)).validate()

    def test_write_capabilities_filter(self):
        names = [cap.name for cap in write_capabilities(contract())]
        self.assertEqual(names, ["create", "update", "delete"])

    def test_validate_many_rejects_duplicate_ids(self):
        with self.assertRaises(ContractError):
            validate_many((contract(), contract()))


class RecoveryGroupTests(unittest.TestCase):
    def test_valid_group(self):
        group = RecoveryGroup(
            group_id="books",
            pool="data",
            parent_dataset="data/books",
            members=(
                DatasetMount(
                    name="books",
                    dataset="data/books/content",
                    mount="/srv/books",
                    writer="write-broker",
                ),
            ),
            required_config=("/etc/data-plane/books.toml",),
        )
        group.validate()

    def test_member_outside_parent_rejected(self):
        group = RecoveryGroup(
            group_id="books",
            pool="data",
            parent_dataset="data/books",
            members=(
                DatasetMount(
                    name="escape", dataset="data/photos", mount="/srv/photos"
                ),
            ),
        )
        with self.assertRaises(ContractError):
            group.validate()

    def test_duplicate_mount_rejected(self):
        member = DatasetMount(name="a", dataset="data/x/a", mount="/srv/a")
        group = RecoveryGroup(
            group_id="x", pool="data", parent_dataset="data/x", members=(member, member)
        )
        with self.assertRaises(ContractError):
            group.validate()


if __name__ == "__main__":
    unittest.main()
