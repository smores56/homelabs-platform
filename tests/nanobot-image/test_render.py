"""Unit tests for the image-side render's memory MCP wiring (SDLC #631).

Imports images/nanobot/render.py by path with the runner's plain python3, so the
image itself needs neither yaml nor nanobot installed: render.py defers both
imports to the functions that use them.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

RENDER = Path(__file__).resolve().parents[2] / "images" / "nanobot" / "render.py"


def load_render():
    spec = importlib.util.spec_from_file_location("nanobot_render", RENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load_render()

BASE_CONFIG = {
    "gateway": {"host": "0.0.0.0"},
    "agents": {"defaults": {"provider": "neuralwatt", "model": "m"}},
    "tools": {"exec": {"timeout": 120}},
}


class MemoryMcpTest(unittest.TestCase):
    def test_import_needs_no_yaml_or_nanobot(self):
        # The CI job runs plain python3; a top-level import of either would
        # break the job rather than the image.
        self.assertNotIn("yaml", sys.modules)
        self.assertNotIn("nanobot", sys.modules)

    def test_absent_broker_url_leaves_config_untouched(self):
        for value in (None, "", "   "):
            with self.subTest(broker_url=value):
                config = copy.deepcopy(BASE_CONFIG)
                original = copy.deepcopy(config)
                render.apply_memory_mcp(config, value.strip() if value else value)
                self.assertEqual(config, original)

    def test_valid_url_adds_the_server_with_the_exact_shape(self):
        config = copy.deepcopy(BASE_CONFIG)
        render.apply_memory_mcp(config, "http://broker.broker.svc.cluster.local:8080")
        self.assertEqual(
            config["tools"]["mcp_servers"]["memory"],
            {
                "type": "streamableHttp",
                "url": "http://broker.broker.svc.cluster.local:8080/memory/mcp",
                "enabled_tools": ["*"],
            },
        )
        # Everything else the render owns survives.
        self.assertEqual(config["tools"]["exec"], {"timeout": 120})
        self.assertEqual(config["gateway"], {"host": "0.0.0.0"})

    def test_https_url_and_missing_tools_block(self):
        config = {}
        render.apply_memory_mcp(config, "https://broker.example")
        self.assertEqual(
            config["tools"]["mcp_servers"]["memory"]["url"],
            "https://broker.example/memory/mcp",
        )

    def test_trailing_slash_is_collapsed(self):
        for broker_url in (
            "http://broker.broker.svc.cluster.local:8080/",
            "http://broker.broker.svc.cluster.local:8080///",
        ):
            with self.subTest(broker_url=broker_url):
                config = {}
                render.apply_memory_mcp(config, broker_url)
                self.assertEqual(
                    config["tools"]["mcp_servers"]["memory"]["url"],
                    "http://broker.broker.svc.cluster.local:8080/memory/mcp",
                )

    def test_malformed_url_fails_loudly(self):
        for broker_url in ("broker.broker.svc.cluster.local:8080", "ftp://broker", "//broker"):
            with self.subTest(broker_url=broker_url):
                with self.assertRaises(SystemExit) as caught:
                    render.apply_memory_mcp({}, broker_url)
                self.assertEqual(caught.exception.code, 1)

    def test_operator_entry_is_preserved(self):
        operator = {
            "type": "sse",
            "url": "http://memory-mcp.graphiti.svc.cluster.local:8182/sse",
            "headers": {"Authorization": "Bearer operator"},
        }
        config = {"tools": {"mcp_servers": {"memory": copy.deepcopy(operator)}}}
        render.apply_memory_mcp(config, "http://broker:8080")
        self.assertEqual(config["tools"]["mcp_servers"]["memory"], operator)


if __name__ == "__main__":
    unittest.main()
