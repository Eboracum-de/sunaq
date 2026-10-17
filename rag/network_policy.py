"""Shared network trust-boundary helpers for private services and public egress."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx


def _private_or_local(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def resolve_private_endpoint(url: str) -> tuple[str, list[str]]:
    """Resolve an HTTP(S) endpoint and require every answer to be private/local."""

    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"private endpoint must be an absolute http(s) URL: {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("userinfo is not allowed in private service URLs")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise RuntimeError(f"private endpoint DNS resolution failed: {host}") from exc
        addresses = sorted({str(item[4][0]) for item in infos if item and item[4]})

    if not addresses:
        raise RuntimeError(f"private endpoint DNS resolution returned no addresses: {host}")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not _private_or_local(address):
            raise RuntimeError(
                f"private endpoint resolved outside the private network: {host} -> {value}"
            )
    return host, addresses


def pinned_private_target(url: str) -> tuple[str, str, dict[str, str] | None]:
    """Return one IP-pinned URL plus Host header and TLS SNI extension.

    Resolution and policy validation happen once, and the actual TCP connection
    is made to that exact IP. This avoids DNS-rebinding between policy check and
    connect for SRC model/reranker endpoints.
    """

    parsed = urlparse(str(url or ""))
    hostname, addresses = resolve_private_endpoint(url)
    address = next(
        (
            value
            for value in addresses
            if ipaddress.ip_address(value).version == 4
        ),
        addresses[0],
    )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    default_port = 443 if parsed.scheme == "https" else 80
    display_host = hostname
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    host_header = display_host if port == default_port else f"{display_host}:{port}"

    pinned = httpx.URL(str(url)).copy_with(host=address)
    extensions = {"sni_hostname": hostname} if parsed.scheme == "https" else None
    return str(pinned), host_header, extensions


def is_public_ip(value: str) -> bool:
    """Return whether an address is globally routable public Internet space."""
    try:
        address = ipaddress.ip_address(str(value or ""))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(address.is_global) and not (
        address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def resolve_host_ips(host: str, port: int) -> list[str]:
    """Resolve a host once for outbound policy validation."""
    host = str(host or "").strip().rstrip(".")
    if not host:
        return []
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror:
        return []
    return sorted({str(item[4][0]) for item in infos if item and item[4]})


def validated_public_connect_targets(
    url: str,
    *,
    allowed_ports: set[int] | None = None,
    allowed_schemes: set[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Return IP-pinned targets for an allowed public HTTP(S) destination.

    DNS is resolved exactly once for validation.  Callers connect to the returned
    IP literal while retaining the original Host header and TLS SNI hostname,
    closing the validation/connect DNS-rebinding window.
    """
    allowed = allowed_schemes or {"http", "https"}
    try:
        parsed = urlparse(str(url or ""))
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise RuntimeError("invalid URL") from exc

    if parsed.scheme not in allowed or not parsed.hostname:
        raise RuntimeError("unsupported/non-HTTP URL")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("userinfo in URLs is not allowed")
    if allowed_ports is not None and port not in allowed_ports:
        raise RuntimeError(f"destination port {port} is not allowed")

    folded = parsed.hostname.casefold().rstrip(".")
    if folded == "localhost" or folded.endswith(".localhost"):
        raise RuntimeError("private/local destination blocked")

    addresses = resolve_host_ips(parsed.hostname, port)
    if not addresses:
        raise RuntimeError("DNS resolution failed")
    if any(not is_public_ip(value) for value in addresses):
        raise RuntimeError("private/local destination blocked")

    default_port = 443 if parsed.scheme == "https" else 80
    display_host = parsed.hostname
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    host_header = display_host if port == default_port else f"{display_host}:{port}"

    original = httpx.URL(str(url))
    return [
        (str(original.copy_with(host=address)), host_header, parsed.hostname)
        for address in addresses
    ]
