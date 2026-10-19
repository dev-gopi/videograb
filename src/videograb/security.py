"""Basic SSRF guard, with explicit protection for redirects and DNS changes.

NOTE: external subprocesses and Chromium are not an OS-level network sandbox.
Never expose this local application on an untrusted/public network.
"""
from __future__ import annotations
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urljoin


class UnsafeURL(ValueError):
    pass


def normalize(url: str, base: str | None = None) -> str:
    url = url.strip().replace("\\/", "/")
    if base:
        url = urljoin(base, url)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeURL("Only public HTTP(S) URLs without embedded credentials are allowed")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeURL("Invalid URL port") from exc
    return url


def allowed_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return ip.is_global and not ip.is_loopback and not ip.is_private and not ip.is_link_local


async def validate(url: str, allow_private: bool = False) -> str:
    url = normalize(url)
    if allow_private:
        return url
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeURL("Local addresses are blocked")
    if host.replace(".", "").isdecimal() and not allowed_ip(host):
        raise UnsafeURL("Non-public host is blocked")
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        try:
            infos = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM), 5)
        except (OSError, asyncio.TimeoutError) as exc:
            raise UnsafeURL(f"Cannot resolve host: {host}") from exc
        addrs = [info[4][0] for info in infos]
    if not addrs or any(not allowed_ip(addr) for addr in addrs):
        raise UnsafeURL("Private, reserved, or mixed public/private DNS results are blocked")
    return url


def safe_name(value: str, max_length: int = 120) -> str:
    import re
    import unicodedata
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return (value[:max_length].rstrip(" .") or "video")
