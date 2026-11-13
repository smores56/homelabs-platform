"""Entrypoint: `python -m webhook`.

A missing token file or an unreadable source store aborts startup (exit 2) so
the pod stays unready rather than accepting anonymous registrations or dropping
every delivery behind a green `/healthz`.
"""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, load
from .dedupe import DedupeCache
from .gateway import GatewayClient, GatewayError
from .server import Application, make_server
from .store import SourceStore, StoreError

LOG = logging.getLogger("webhook")


def run(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print("usage: python -m webhook", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load()
        store = SourceStore(config.data_dir / "sources.json")
        store.load()
        gateway = GatewayClient(config.gateway_url, config.gateway_token)
    except (ConfigError, StoreError, GatewayError) as exc:
        print(f"webhook: {exc}", file=sys.stderr)
        return 2
    application = Application(
        store=store,
        dedupe=DedupeCache(),
        enqueue=gateway.enqueue,
        admin_token=config.admin_token,
        public_url=config.public_url,
    )
    server = make_server(application, config.address, config.port)
    LOG.info(
        "listening on %s:%d sources=%d gateway=%s",
        config.address,
        config.port,
        len(tuple(store.sources())),
        config.gateway_url,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(run())
