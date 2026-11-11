"""Target integrity: DNS pinning, address policy, redirects, request shape.

Every denial happens before a credential is attached. The resolver is injectable
so the tests can drive each branch without touching the network; production uses
`socket.getaddrinfo` exactly once per request and dials the address it checked.
"""

from __future__ import annotations

import ipaddress
import posixpath
import socket
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[..., list[tuple]]

# Refused for every enrollment, including a private one: these never appear in a
# legitimate upstream address, and the pod CIDR is where the agent itself lives.
ALWAYS_DENIED = tuple(
    ipaddress.ip_network(item)
    for item in (
        "0.0.0.0/8",
        "10.42.0.0/16",  # k3s pod CIDR
        "127.0.0.0/8",
        "100.64.0.0/10",  # CGNAT; no upstream legitimately answers here
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fe80::/10",
        "ff00::/8",
    )
)
# Refused unless the enrollment is an explicit private one (the in-cluster Loki
# gateway and status exporter). RFC1918, its IPv6 analogue, CGNAT and the
# service CIDR are all here.
PRIVATE = tuple(
    ipaddress.ip_network(item)
    for item in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "fc00::/7",
    )
)
SERVICE_CIDR = ipaddress.ip_network("10.43.0.0/16")
LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


class Denied(Exception):
    """A target failed integrity. `reason` is a stable, log-safe token."""

    def __init__(self, reason: str, status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class PinnedAddress:
    ip: Address
    family: int


def _in_networks(address: Address, networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    return any(address in network for network in networks if network.version == address.version)


def address_allowed(
    address: str | Address,
    *,
    allow_private: bool,
    deny_addresses: Iterable[str | Address] = (),
) -> bool:
    """Whether the broker may dial this address for this enrollment."""
    if isinstance(address, str):
        address = ipaddress.ip_address(address)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # ::ffff:10.0.0.1 is the same destination as 10.0.0.1.
        address = address.ipv4_mapped
    if _in_networks(address, ALWAYS_DENIED):
        return False
    if address in {_address(item) for item in deny_addresses}:
        return False
    if _in_networks(address, PRIVATE):
        # The service CIDR is reachable only from a private enrollment; a pod
        # address (10.42/16) was already refused above.
        return allow_private
    return address.is_global


def _address(value: str | Address) -> Address:
    return ipaddress.ip_address(value) if isinstance(value, str) else value


def resolve_pinned(
    host: str,
    port: int,
    *,
    allow_private: bool,
    deny_addresses: Iterable[str | Address] = (),
    resolver: Resolver = socket.getaddrinfo,
) -> PinnedAddress:
    """Resolve once and return the address to dial.

    Any refused address in the answer set is a denial, not a reason to try the
    next one: a DNS answer that mixes a public and a private address is a
    rebinding attempt (spec 04, 6.4).
    """
    if host.lower() in LOOPBACK_NAMES:
        raise Denied("loopback_name")
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise Denied("dns_failure", status=502) from None
    seen: list[tuple[Address, int]] = []
    for family, _type, _proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        except ValueError:
            raise Denied("dns_unparsable", status=502) from None
        if (address, family) in seen:
            continue
        seen.append((address, family))
    if not seen:
        raise Denied("dns_empty", status=502)
    for address, _family in seen:
        if not address_allowed(address, allow_private=allow_private, deny_addresses=deny_addresses):
            raise Denied(f"address_refused:{address}")
    address, family = seen[0]
    return PinnedAddress(ip=address, family=family)


def check_redirect(location: str, *, scheme: str, host: str, port: int) -> None:
    """A 3xx may only point at the same real endpoint.

    Cross-host redirects are returned to the agent as a denial because following
    one would either leak the credential or silently rebind the logical host
    (spec 04, 6.6). The broker never follows any redirect itself.
    """
    try:
        target = urlsplit(location.strip())
        target_port = target.port
    except ValueError:
        raise Denied("redirect_scheme") from None
    if not target.scheme and not target.netloc:
        return  # relative: same origin by construction
    if target.scheme and target.scheme not in ("http", "https"):
        raise Denied("redirect_scheme")
    if target.scheme != scheme:
        raise Denied("redirect_scheme")
    target_host = (target.hostname or "").lower()
    if target_host != host.lower():
        raise Denied("cross_host_redirect", status=502)
    target_port = target_port or (443 if target.scheme == "https" else 80)
    if target_port != port:
        raise Denied("cross_host_redirect", status=502)


def split_target(raw_target: str, host_header: str | None) -> tuple[str, str, str]:
    """Split `/<logical-host>/<upstream-path>?<query>` and validate the shape.

    Returns (logical_host, upstream_path, query). Absolute-form targets, an
    IP-literal Host, and `//`-ambiguous paths are refused: the agent may name a
    logical host and a path, never a destination (spec 04, 6.3 step 2).
    """
    if raw_target.startswith(("http://", "https://")):
        raise Denied("absolute_form", status=400)
    if not raw_target.startswith("/"):
        raise Denied("ambiguous_target", status=400)
    if raw_target.startswith("//"):
        raise Denied("ambiguous_target", status=400)
    if not host_header:
        raise Denied("missing_host", status=400)
    host_only = host_header.strip()
    if host_only.startswith("["):
        raise Denied("ip_literal_host", status=400)
    host_only = host_only.rsplit(":", 1)[0] if host_only.count(":") == 1 else host_only
    try:
        ipaddress.ip_address(host_only)
    except ValueError:
        pass
    else:
        raise Denied("ip_literal_host", status=400)

    path, separator, query = raw_target.partition("?")
    try:
        decoded = unquote(path, errors="strict")
    except ValueError:
        raise Denied("ambiguous_path", status=400) from None
    if "\x00" in decoded or "\\" in decoded:
        raise Denied("ambiguous_path", status=400)
    logical, _sep, rest = decoded.lstrip("/").partition("/")
    if not logical:
        raise Denied("missing_logical_host", status=404)
    # Canonicalize dot segments once; the enrolled upstream and the agent then
    # agree on which path is being requested.
    upstream_path = posixpath.normpath("/" + rest)
    if upstream_path == "/.":
        upstream_path = "/"
    if rest.endswith("/") and not upstream_path.endswith("/"):
        upstream_path += "/"
    return logical, upstream_path, query if separator else ""
