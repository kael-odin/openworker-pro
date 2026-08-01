"""Bounded, vendor-restricted async transport for the iLink service."""

from __future__ import annotations

import base64
import ipaddress
import json
import secrets
import socket
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 0
_VENDOR_ROOTS = ("weixin.qq.com", "weixin.com", "wechat.com")

# RFC 2544 benchmarking range (198.18.0.0/15). Python's `is_private` flags it as private, so the
# generic guard rejects it. But local transparent proxies (Clash/V2Ray fake-ip mode) routinely
# map public vendor hostnames into this range as a routing hint — the connection is then tunneled
# to the real public host by the proxy, NOT to a private service. For a *fixed vendor origin*
# (already pinned to Tencent's WeChat domains, HTTPS-only, no user-controlled host), this is a
# legitimate local-networking setup, not an SSRF vector. We still hard-block the ranges that are
# genuinely dangerous even through a proxy: loopback, link-local (cloud metadata 169.254.169.254),
# and unspecified. CGNAT (100.64/10) is left to the proxy too — a fake-ip there is the same
# routing-hint situation, and a real CGNAT host is not the machine's own network position.
_FAKE_IP_RANGES = (
    ipaddress.ip_network("198.18.0.0/15"),  # RFC 2544 — Clash/V2Ray fake-ip default
)
# RFC 6598 shared address space (CGNAT). Python's is_private misses it; refuse it explicitly so
# a DNS-aimed hit at 100.64.0.0/10 is not treated as "not private" and allowed through.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _vendor_blocked_reason(ip: ipaddress._BaseAddress) -> Optional[str]:
    """Address gate for a *fixed vendor origin* (host already pinned to Tencent WeChat domains).

    The generic web guard rejects everything private/reserved because a model-chosen URL can be
    aimed at the machine's own network position. Here the host is NOT user-controlled — it
    passed ``_vendor_host`` (pinned to Tencent WeChat domains) + HTTPS + port 443 — so an
    internal-range answer cannot be an SSRF aim; it can only be a local transparent proxy's
    fake-ip routing hint (Clash/V2Ray map public hosts into 198.18.0.0/15, then tunnel the
    connection to the real public host). We allow that one carve-out so iLink works behind a
    proxy, but keep refusing every range that is genuinely dangerous even through a proxy:
    loopback, link-local (cloud metadata 169.254.169.254), unspecified, multicast, RFC1918,
    CGNAT, and reserved ranges other than the fake-ip carve-out. A fake-ip proxy never maps a
    public host into those, so a legitimate proxy connection is never refused.
    """
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (includes the cloud metadata endpoint)"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_multicast:
        return "multicast"
    # The one carve-out: Clash/V2Ray fake-ip mode (RFC 2544, 198.18.0.0/15). Everything else
    # private/reserved is still refused — RFC1918, CGNAT, and 240.0.0.0/4 are not fake-ip ranges
    # any default proxy uses, and allowing them would widen the surface for a DNS-aimed hit.
    if any(ip in net for net in _FAKE_IP_RANGES):
        return None
    if ip.is_private:
        return "a private network"
    if ip.version == 4 and ip in _CGNAT:
        return "shared address space (CGNAT / RFC 6598)"
    if ip.is_reserved:
        return "a reserved range"
    return None


class IlinkTransportError(RuntimeError):
    """A stable public error code; response text and credentials stay internal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _vendor_host(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    return any(normalized == root or normalized.endswith("." + root) for root in _VENDOR_ROOTS)


def _normalize_ip(raw: str) -> Optional[ipaddress._BaseAddress]:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def validate_base_url(
    url: str,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> str:
    """Return a canonical vendor origin, or raise without reflecting the input URL."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise IlinkTransportError("invalid_base_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except (ValueError, UnicodeError):
        raise IlinkTransportError("invalid_base_url") from None
    host = (parts.hostname or "").rstrip(".").lower()
    if (
        parts.scheme.lower() != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not _vendor_host(host)
        or parts.query
        or parts.fragment
    ):
        raise IlinkTransportError("invalid_base_url")

    # A baseurl is an origin in the observed protocol.  A trailing slash is tolerated,
    # but path prefixes would make endpoint construction ambiguous.
    if parts.path not in ("", "/"):
        raise IlinkTransportError("invalid_base_url")

    literal = _normalize_ip(host)
    if literal is not None:
        # Literal IPs cannot satisfy the vendor host policy, even when public.
        raise IlinkTransportError("invalid_base_url")

    try:
        infos = resolver(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise IlinkTransportError("base_url_unresolvable") from None
    if not infos:
        raise IlinkTransportError("base_url_unresolvable")
    for info in infos:
        ip = _normalize_ip(str(info[4][0]))
        if ip is None or _vendor_blocked_reason(ip):
            raise IlinkTransportError("base_url_blocked")

    return urlunsplit(("https", host, "", "", ""))


def auth_headers(bot_token: str = "") -> dict[str, str]:
    number = secrets.randbits(32)
    uin = base64.b64encode(str(number).encode("ascii")).decode("ascii")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
    }
    if bot_token:
        headers["Authorization"] = f"Bearer {bot_token}"
    return headers


class IlinkTransport:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client: Optional[httpx.AsyncClient] = None,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.base_url = validate_base_url(base_url, resolver=resolver)
        self.max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(connect=10.0, read=50.0, write=10.0, pool=10.0),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise IlinkTransportError("invalid_endpoint")
        return self.base_url + path

    async def get(
        self,
        path: str,
        *,
        params: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        return await self._request("GET", path, params=params, headers=headers)

    async def post(
        self,
        path: str,
        *,
        bot_token: str,
        json_body: dict[str, Any],
    ) -> Any:
        if not bot_token:
            raise IlinkTransportError("missing_bot_token")
        return await self._request(
            "POST", path, headers=auth_headers(bot_token), json_body=json_body
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            request = self._client.build_request(
                method,
                self._url(path),
                params=params,
                headers=headers,
                json=json_body,
            )
            response = await self._client.send(request, stream=True)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
            raise IlinkTransportError("network_error") from None

        try:
            if response.is_redirect:
                raise IlinkTransportError("redirect_refused")
            if response.status_code < 200 or response.status_code >= 300:
                raise IlinkTransportError("http_error")
            length = response.headers.get("content-length")
            if length:
                try:
                    if int(length) > self.max_response_bytes:
                        raise IlinkTransportError("response_too_large")
                except ValueError:
                    raise IlinkTransportError("invalid_response") from None

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self.max_response_bytes:
                    raise IlinkTransportError("response_too_large")
                chunks.append(chunk)
            try:
                raw = b"".join(chunks).decode("utf-8")
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise IlinkTransportError("invalid_json") from None
            if not isinstance(value, dict):
                raise IlinkTransportError("invalid_response")
            return value
        finally:
            await response.aclose()
