"""Inbound webhook receiver for the cluster agent (SDLC #627).

One public listener per delivery source. A capability URL identifies the
source, an optional GitHub HMAC authenticates the body, delivery ids dedupe
retries, and one enqueue call hands the text to nanobot's gateway. The receiver
holds no provider credential and has no other upstream verb.
"""

from .dedupe import DedupeCache
from .gateway import GatewayClient, GatewayError
from .server import Application, BodyError, Response, make_handler, make_server, render_message
from .signature import SIGNATURE_HEADER, verify
from .store import Source, SourceStore, StoreError, new_capability

__all__ = [
    "Application",
    "BodyError",
    "DedupeCache",
    "GatewayClient",
    "GatewayError",
    "Response",
    "SIGNATURE_HEADER",
    "Source",
    "SourceStore",
    "StoreError",
    "make_handler",
    "make_server",
    "new_capability",
    "render_message",
    "verify",
]
