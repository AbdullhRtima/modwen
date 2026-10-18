"""LAN access helpers: which host names the server accepts and which URLs other devices can use.

LAN access is opt-in (``app.lan_access`` or ``modwen serve --lan``). Even then every API call needs the
access token, and only this machine's own addresses (plus ``app.allowed_hosts``) are accepted as Host.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}


def local_ipv4() -> list[str]:
    """This machine's private/LAN IPv4 addresses (e.g. 192.168.100.30), excluding loopback and link-local."""
    import psutil

    out = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(a.address)
            if ip.is_loopback or ip.is_link_local:
                continue
            if ip.is_private:
                out.append(a.address)
    return sorted(set(out))


def machine_names() -> list[str]:
    names = set()
    try:
        host = socket.gethostname()
        if host:
            names.add(host.lower())
            short = host.split(".")[0].lower()
            names.update({short, f"{short}.local"})
    except OSError:
        pass
    return sorted(names)


def allowed_hosts(cfg, lan: bool) -> set[str]:
    hosts = set(LOCAL_HOSTS)
    if lan:
        hosts.update(local_ipv4())
        hosts.update(machine_names())
        hosts.update(h.lower() for h in cfg.app.allowed_hosts)
    return hosts


def host_of(header_value: str | None) -> str:
    """Host header -> bare host (handles 'host:port' and '[::1]:port')."""
    v = (header_value or "").strip().lower()
    if v.startswith("["):
        return v.split("]")[0] + "]"
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def origin_allowed(origin: str | None, hosts: set[str]) -> bool:
    """Same-machine origins only: the browser page must have been served by one of our host names."""
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host == "::1":
        host = "[::1]"
    return parsed.scheme in ("http", "https") and host in hosts


def lan_urls(port: int, token: str | None = None) -> list[str]:
    q = f"/?token={token}" if token else "/"
    names = [n for n in machine_names() if n.endswith(".local")]
    return [f"http://{ip}:{port}{q}" for ip in local_ipv4()] + [f"http://{n}:{port}{q}" for n in names]
