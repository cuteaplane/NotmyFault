from __future__ import annotations

import ipaddress
import socket
from urllib.parse import ParseResult, urlparse


_PROXY_FAKE_IPV4_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def resolve_host_addresses(
    host: str,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    host = host.lower().rstrip(".")
    try:
        return (ipaddress.ip_address(host),)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as error:
            raise ValueError("URL 主机无法解析") from error
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in resolved
            if isinstance(item[4][0], str)
        }
        if not addresses:
            raise ValueError("URL 主机无法解析")
        return tuple(
            sorted(
                addresses,
                key=lambda address: (address.version, int(address)),
            )
        )


def is_private_host(host: str, *, allow_proxy_fake_ip: bool = False) -> bool:
    host = host.lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addresses = resolve_host_addresses(host)
    except ValueError:
        return True
    for address in addresses:
        if (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
        ):
            address = address.ipv4_mapped
        if (
            allow_proxy_fake_ip
            and isinstance(address, ipaddress.IPv4Address)
            and address in _PROXY_FAKE_IPV4_NETWORK
        ):
            continue
        if not address.is_global:
            return True
    return False


def resolve_public_http_url(
    url: str,
    *,
    https_only: bool = False,
    allow_proxy_fake_ip: bool = False,
) -> tuple[ParseResult, tuple[str, ...]]:
    parsed = urlparse(url)
    schemes = ("https",) if https_only else ("http", "https")
    if parsed.scheme.lower() not in schemes:
        expected = "https" if https_only else "http/https"
        raise ValueError(f"仅允许 {expected} URL")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL 主机格式无效")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL 端口格式无效") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL 端口格式无效")
    addresses = resolve_host_addresses(parsed.hostname)
    public_addresses = []
    for raw_address in addresses:
        address = raw_address
        if (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
        ):
            address = address.ipv4_mapped
        if (
            allow_proxy_fake_ip
            and isinstance(address, ipaddress.IPv4Address)
            and address in _PROXY_FAKE_IPV4_NETWORK
        ):
            public_addresses.append(str(address))
            continue
        if not address.is_global:
            raise ValueError("URL 不能指向本机、内网或保留地址")
        public_addresses.append(str(address))
    return parsed, tuple(dict.fromkeys(public_addresses))


def validate_public_http_url(
    url: str,
    *,
    https_only: bool = False,
    allow_proxy_fake_ip: bool = False,
) -> ParseResult:
    parsed, _addresses = resolve_public_http_url(
        url,
        https_only=https_only,
        allow_proxy_fake_ip=allow_proxy_fake_ip,
    )
    return parsed
