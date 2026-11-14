#!/usr/bin/env python3
"""Render the deployed collector schemas from the packaged descriptors.

The four collector Deployments mount a ConfigMap built from
``deploy/data-plane/config/sources/*.json``. Those files must be the exact
contract the runtime loader validates, so generate them from the descriptors
instead of maintaining a second copy by hand.

    scripts/render-source-schemas.py          # rewrite
    scripts/render-source-schemas.py --check  # fail if stale
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DESCRIPTORS = ROOT / "libs" / "data_plane" / "collectors" / "schemas"
DEPLOYED = ROOT / "deploy" / "data-plane" / "config" / "sources"

# Deployed source name -> packaged descriptor name. tuta is the mail source's
# provider name; the descriptor already carries the `tuta.mail` source const.
SOURCES = {
    "brave": "brave",
    "finance": "finance",
    "immich": "immich",
    "tuta": "mail",
}


def rendered(deploy_name: str, descriptor_name: str) -> str:
    return (DESCRIPTORS / f"{descriptor_name}.json").read_text(encoding="utf-8")


def stale() -> list[str]:
    changed: list[str] = []
    for deploy_name, descriptor_name in SOURCES.items():
        target = DEPLOYED / f"{deploy_name}.json"
        expected = rendered(deploy_name, descriptor_name)
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != expected:
            changed.append(deploy_name)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing")
    args = parser.parse_args(argv)
    if args.check:
        changed = stale()
        if changed:
            print(f"stale collector schemas: {', '.join(changed)}", file=sys.stderr)
            return 1
        return 0
    DEPLOYED.mkdir(parents=True, exist_ok=True)
    for deploy_name, descriptor_name in SOURCES.items():
        (DEPLOYED / f"{deploy_name}.json").write_text(
            rendered(deploy_name, descriptor_name), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
