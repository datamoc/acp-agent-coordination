"""Helpers shared by the server and the client."""

import ipaddress
import urllib.parse


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def prefer_loopback_ipv4(url: str) -> str:
    """Connect to 127.0.0.1 when the URL names exactly `localhost`.

    The server binds IPv4 only by default, while on some systems (Windows)
    `localhost` resolves to ::1 first - and ::1 may accept the connection
    without answering instead of refusing it, so urllib tries every
    getaddrinfo result and each request stalls (~2 s measured). The server
    certificate covers both names (DNS:localhost, IP:127.0.0.1), so the
    rewrite keeps TLS hostname verification passing. When the server
    listens on IPv6 instead (--listen ::1), connect with the literal
    address: this rewrite would send you to the wrong family.
    """
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "") != "localhost":
        return url
    host = "127.0.0.1"
    if parts.port:
        host += f":{parts.port}"
    netloc = host
    if parts.username:
        auth = parts.username + (f":{parts.password}" if parts.password else "") + "@"
        netloc = auth + host
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
