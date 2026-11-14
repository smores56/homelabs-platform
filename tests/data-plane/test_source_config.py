"""The deployed collector ConfigMaps must load and match the descriptors."""

from __future__ import annotations

import unittest
from pathlib import Path

from data_plane.collectors.schemas import load_descriptor_from_file

ROOT = Path(__file__).resolve().parents[2]
DEPLOYED = ROOT / "deploy" / "data-plane" / "config" / "sources"
DESCRIPTORS = ROOT / "libs" / "data_plane" / "collectors" / "schemas"
SOURCES = {"brave": "brave", "finance": "finance", "immich": "immich", "tuta": "mail"}


class DeployedSchemaTests(unittest.TestCase):
    def test_every_deployed_schema_loads(self):
        for deploy_name, descriptor_name in SOURCES.items():
            with self.subTest(source=deploy_name):
                descriptor = load_descriptor_from_file(
                    DEPLOYED / f"{deploy_name}.json", name=deploy_name
                )
                self.assertEqual(
                    descriptor.name, deploy_name
                )
                self.assertEqual(
                    descriptor.required_fields,
                    load_descriptor_from_file(
                        DESCRIPTORS / f"{descriptor_name}.json",
                        name=descriptor_name,
                    ).required_fields,
                )

    def test_deployed_files_match_the_descriptors(self):
        for deploy_name, descriptor_name in SOURCES.items():
            with self.subTest(source=deploy_name):
                self.assertEqual(
                    (DEPLOYED / f"{deploy_name}.json").read_text(encoding="utf-8"),
                    (DESCRIPTORS / f"{descriptor_name}.json").read_text(
                        encoding="utf-8"
                    ),
                )
