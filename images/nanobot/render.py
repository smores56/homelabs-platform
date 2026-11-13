"""Render nanobot's runtime state from the mounted declarative tree.

`$NANOBOT_CONFIG_DIR` is split by ownership: the platform ConfigMap
`nanobot-policy` supplies `prompts/system.md` and `models/providers.yaml`, the
tenant repository supplies `skills/` and `cron/`. This file is the image-side
half of that contract (ported from pkgs/nanobot/render.py in nix-config). It
refuses to start on a missing or unparsable input rather than falling back to a
default model. It runs on every container start, so the tree is the only source
of the agent's behaviour.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path


def fail(message: str) -> None:
    print(f"nanobot-render: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_yaml(path: Path) -> object:
    import yaml

    try:
        return yaml.safe_load(path.read_text())
    except Exception as exc:  # noqa: BLE001 - any parser error is fatal
        fail(f"{path}: {exc}")


def integer(spec: dict, key: str, label: str) -> int:
    value = spec.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        fail(f"{label}: {key} must be a positive integer")
    return value


def main() -> int:
    config_dir = Path(os.environ.get("NANOBOT_CONFIG_DIR", "/etc/nanobot"))
    home = Path(os.environ.get("HOME", "/data/home"))

    providers_path = config_dir / "models" / "providers.yaml"
    prompt_path = config_dir / "prompts" / "system.md"
    skills_src = config_dir / "skills"
    cron_src = config_dir / "cron"
    for path in (providers_path, prompt_path):
        if not path.is_file():
            fail(f"missing {path}")
    if not skills_src.is_dir():
        fail(f"missing {skills_src}")

    prompt = prompt_path.read_text()
    if not prompt.strip():
        fail(f"{prompt_path} is empty")

    doc = load_yaml(providers_path)
    if not isinstance(doc, dict):
        fail(f"{providers_path}: not an object")
    provider = doc.get("provider")
    api_base = doc.get("api_base")
    models = doc.get("models") if isinstance(doc.get("models"), dict) else {}
    default = models.get("default")
    catalog = models.get("catalog") if isinstance(models.get("catalog"), dict) else {}
    if not isinstance(provider, str) or not provider:
        fail(f"{providers_path}: provider is required")
    if not isinstance(api_base, str) or not api_base.startswith(("http://", "https://")):
        fail(f"{providers_path}: api_base must be an http(s) URL")
    if default not in catalog:
        fail(f"{providers_path}: default model {default!r} is not in the catalog")
    default_spec = catalog[default]
    if not isinstance(default_spec, dict):
        fail(f"{providers_path}: {default} must be an object")
    context_window = integer(default_spec, "context_window", default)
    max_tokens = integer(default_spec, "max_output_tokens", default)

    from nanobot.cli.docker_bootstrap import initialize_config

    config_path = home / ".nanobot" / "config.json"
    if not config_path.exists():
        initialize_config(config_path)
    try:
        config = json.loads(config_path.read_text())
    except Exception as exc:  # noqa: BLE001
        fail(f"{config_path}: {exc}")

    # The pod is reached through the tunnel, so both listeners bind everywhere.
    config.setdefault("gateway", {})["host"] = "0.0.0.0"
    config.setdefault("channels", {}).setdefault("websocket", {})["host"] = "0.0.0.0"
    # The broker injects the real key; nanobot only needs a non-empty value.
    config.setdefault("providers", {})[provider] = {"api_base": api_base, "api_key": "none"}
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    defaults.update(
        {
            "provider": provider,
            "model": default,
            "context_window_tokens": context_window,
            "max_tokens": max_tokens,
        }
    )
    presets = config.setdefault("modelPresets", {})
    for model_id, spec in catalog.items():
        if not isinstance(spec, dict):
            fail(f"{providers_path}: catalog entry {model_id} must be an object")
        presets[model_id] = {
            "model": model_id,
            "provider": provider,
            "context_window_tokens": integer(spec, "context_window", model_id),
            "max_tokens": integer(spec, "max_output_tokens", model_id),
        }
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    workspace = home / ".nanobot" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(prompt if prompt.endswith("\n") else prompt + "\n")

    skills_dst = workspace / "skills"
    if skills_dst.exists():
        shutil.rmtree(skills_dst)
    shutil.copytree(skills_src, skills_dst)

    jobs: list[dict] = []
    if cron_src.is_dir():
        now = int(time.time() * 1000)
        for path in sorted(cron_src.glob("*.yaml")):
            spec = load_yaml(path)
            if not isinstance(spec, dict):
                fail(f"{path}: not an object")
            for key in ("name", "schedule", "prompt"):
                if not isinstance(spec.get(key), str) or not spec[key].strip():
                    fail(f"{path}: {key} is required")
            declared = spec.get("model")
            if not isinstance(declared, str) or declared not in catalog:
                fail(f"{path}: model must name a catalog model")
            if declared != default:
                fail(
                    f"{path}: nanobot 0.4.1 runs cron turns on the default model "
                    f"({default}); {declared} cannot be honored"
                )
            message = spec["prompt"]
            delivery = spec.get("delivery") or {"kind": "file"}
            if not isinstance(delivery, dict) or delivery.get("kind") != "file":
                fail(f"{path}: only delivery kind 'file' is supported")
            outbox = delivery.get("path")
            if isinstance(outbox, str) and outbox.startswith("/data/outbox/"):
                message = f"{message.rstrip()}\n\nWrite the result to {outbox}.\n"
            else:
                fail(f"{path}: delivery.path must be under /data/outbox/")
            jobs.append(
                {
                    "id": spec["name"],
                    "name": spec["name"],
                    "enabled": bool(spec.get("enabled", True)),
                    "schedule": {
                        "kind": "cron",
                        "at_ms": None,
                        "every_ms": None,
                        "expr": spec["schedule"],
                        "tz": None,
                    },
                    "payload": {
                        "kind": "agent_turn",
                        "message": message,
                        "deliver": False,
                        # nanobot binds an agent job only when a legacy
                        # channel/to pair is present; the service then clears
                        # them and runs the turn in that session.
                        "channel": "websocket",
                        "to": "cron",
                        "channel_meta": {},
                        "session_key": None,
                        "origin_channel": None,
                        "origin_chat_id": None,
                        "origin_metadata": {},
                        "binding_version": 0,
                    },
                    "state": {
                        "next_run_at_ms": None,
                        "last_run_at_ms": None,
                        "last_status": None,
                        "last_error": None,
                        "run_history": [],
                    },
                    "created_at_ms": now,
                    "updated_at_ms": now,
                    "delete_after_run": False,
                }
            )
    cron_dst = workspace / "cron"
    cron_dst.mkdir(parents=True, exist_ok=True)
    (cron_dst / "jobs.json").write_text(json.dumps({"version": 1, "jobs": jobs}, indent=2) + "\n")

    skill_count = len(list(skills_dst.glob("*/SKILL.md")))
    print(
        f"nanobot-render: provider={provider} model={default} "
        f"skills={skill_count} cron={len(jobs)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
