"""HTTP access for root tools: one adapter, one egress guard, no hidden redirects.

The guard is the important part. A model can ask a tool to fetch any string, so
``fetch_url`` must assume the URL is hostile (HARD RULE 4):

* only ``http``/``https``, no embedded credentials, standard ports only;
* the hostname must resolve to public addresses - loopback, private, link-local,
  multicast, reserved and cloud-metadata ranges are refused **before** a request;
* every redirect hop is checked again, because ``public.example -> 169.254.169.254``
  is the classic SSRF bypass;
* bodies are capped in bytes, and the caller caps the text it keeps.

``httpx`` is isolated in :class:`HttpxClient`; tests inject a fake ``HTTPClient``
and never touch the network (HARD RULE 5). The DNS check-then-connect window is a
known residual risk: production should additionally egress through a proxy that
enforces the same policy. That is why the resolver is injectable and the vendor
detail is marked ``# VERIFY``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.tools.errors import HostResolutionError, HTTPFetchError, ToolURLBlocked

__all__ = [
    "ALLOWED_PORTS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_S",
    "HTTPClient",
    "HTTPResponse",
    "HostResolver",
    "HttpxClient",
    "SystemHostResolver",
    "ensure_public_url",
]

DEFAULT_TIMEOUT_S: Final[float] = 20.0
DEFAULT_MAX_BYTES: Final[int] = 1_000_000
ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443})

#: Hostnames that never leave the machine, whatever DNS says.
BLOCKED_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})
BLOCKED_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
)

DEFAULT_USER_AGENT: Final[str] = "ScholarLoop/0.1 (+https://github.com/Armannay)"


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    """The small, transport-neutral slice of a response tools are allowed to see."""

    status_code: int
    text: str
    content_type: str
    final_url: str
    location: str | None = None
    truncated: bool = False


class HTTPClient(Protocol):
    """Minimal async GET used by the Crossref adapter and ``fetch_url``.

    Implementations must not follow redirects: the caller validates each hop.
    """

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> HTTPResponse: ...


class HostResolver(Protocol):
    """Resolve a hostname to its addresses so the egress guard can inspect them."""

    async def resolve(self, host: str) -> tuple[str, ...]: ...


class SystemHostResolver:
    """Resolve through the platform resolver, off the event loop."""

    async def resolve(self, host: str) -> tuple[str, ...]:
        """Return every address the host resolves to, or raise ``HostResolutionError``."""
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise HostResolutionError(f"host {host!r} does not resolve") from exc
        addresses = {str(info[4][0]) for info in infos}
        return tuple(sorted(addresses))


class HttpxClient:
    """Production ``HTTPClient``.

    ``# VERIFY``: httpx 0.28 spells the streaming API ``client.stream`` and exposes
    the response charset as ``charset_encoding``. Re-check both when bumping httpx.
    """

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        # Tests inject an ``httpx.MockTransport``; production leaves this ``None``.
        self._transport = transport

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> HTTPResponse:
        """GET ``url`` with a byte cap and no redirect following.

        Raises:
            HTTPFetchError: timeout or transport failure.
        """
        request_headers = {"user-agent": self.user_agent}
        if headers:
            request_headers.update({key.lower(): value for key, value in headers.items()})
        try:
            async with (
                httpx.AsyncClient(
                    timeout=timeout_s,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client,
                client.stream(
                    "GET",
                    url,
                    params=dict(params) if params else None,
                    headers=request_headers,
                ) as response,
            ):
                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    remaining = self.max_bytes - total
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        total = self.max_bytes
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                charset = response.charset_encoding or "utf-8"
                body = b"".join(chunks)
                return HTTPResponse(
                    status_code=response.status_code,
                    text=body.decode(charset, errors="replace"),
                    content_type=response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower(),
                    final_url=str(response.url),
                    location=response.headers.get("location"),
                    truncated=truncated,
                )
        except httpx.TimeoutException as exc:
            raise HTTPFetchError("timeout") from exc
        except httpx.HTTPError as exc:
            # The exception text can contain the full URL; keep only its class name.
            raise HTTPFetchError("transport_error", detail=type(exc).__name__) from exc


def _is_blocked_hostname(host: str) -> bool:
    lowered = host.rstrip(".").lower()
    if lowered in BLOCKED_HOSTS:
        return True
    return any(lowered.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES)


def _ip_literals(host: str) -> tuple[str, ...]:
    """Return ``host`` when it is already an IP literal, else an empty tuple."""
    candidate = host.split("%", 1)[0]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return ()
    return (candidate,)


def _require_public(address: str) -> None:
    """Refuse any address that is not globally routable."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        # An address the guard cannot parse is not evidence of safety.
        raise ToolURLBlocked(f"unparseable address {address!r}") from exc
    if not ip.is_global:
        raise ToolURLBlocked(f"address {ip} is not publicly routable")


async def ensure_public_url(
    url: str,
    *,
    resolver: HostResolver,
    allowed_ports: Collection[int] = ALLOWED_PORTS,
) -> str:
    """Validate ``url`` against the egress policy and return a normalised URL.

    The fragment is dropped (it never reaches a server), embedded credentials are
    refused, and a missing path becomes ``/`` so two spellings of one URL fingerprint
    alike.

    Raises:
        ToolURLBlocked: scheme, credentials, hostname, port or address is not allowed.
        HostResolutionError: the hostname does not resolve.
    """
    if not isinstance(url, str) or not url.strip():
        raise ToolURLBlocked("url must be a non-empty string")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise ToolURLBlocked("url is malformed") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ToolURLBlocked(f"scheme {parts.scheme!r} is not http(s)")
    if parts.username is not None or parts.password is not None:
        raise ToolURLBlocked("embedded credentials are not allowed in a tool URL")

    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ToolURLBlocked("url has no hostname")
    if _is_blocked_hostname(host):
        raise ToolURLBlocked(f"host {host!r} is local-only")

    try:
        port = parts.port
    except ValueError as exc:
        raise ToolURLBlocked("url has an invalid port") from exc
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if effective_port not in allowed_ports:
        raise ToolURLBlocked(f"port {effective_port} is not permitted for tool access")

    addresses = _ip_literals(host)
    if not addresses:
        addresses = await resolver.resolve(host)
    if not addresses:
        raise HostResolutionError(f"host {host!r} resolved to no addresses")
    for address in addresses:
        _require_public(address)

    netloc = host if port is None else f"{host}:{effective_port}"
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]" if port is None else f"[{host}]:{effective_port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
