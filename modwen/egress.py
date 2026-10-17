"""Egress gate: Modwen only talks to services on this machine.

Every component that opens a network connection (LLM client, any future STT client) must call
:func:`ensure_local` first. A non-loopback URL raises :class:`EgressBlocked`; there is no override.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class EgressBlocked(RuntimeError):
    """Raised when something tries to reach a non-local address."""


def is_loopback_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ensure_local(url: str, what: str = "endpoint") -> str:
    if not is_loopback_url(url):
        raise EgressBlocked(
            f"Blocked {what} '{url}': Modwen is local-only and refuses non-loopback addresses. "
            "Use a server on localhost / 127.0.0.1 (e.g. llama.cpp or Ollama)."
        )
    return url
