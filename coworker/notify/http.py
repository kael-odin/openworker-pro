"""受限 webhook POST：逐跳 URL/DNS 校验、vendor host policy、响应上限和错误脱敏。"""

from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from ..web.guard import _blocked_reason

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 3
_MAX_RESPONSE_BYTES = 256 * 1024
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
_BLOCKED_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

VENDOR_HOSTS: dict[str, tuple[str, ...]] = {
    "dingtalk": ("oapi.dingtalk.com",),
    "feishu": ("open.feishu.cn", "open.larksuite.com"),
    "wecom": ("qyapi.weixin.qq.com",),
}


class WebhookPostError(RuntimeError):
    """安全、稳定、不会包含目标 URL 或响应体的 webhook 错误。"""


def _host_allowed(host: str, allowed_hosts: Optional[Iterable[str]]) -> bool:
    if allowed_hosts is None:
        return True
    value = host.rstrip(".").lower()
    return any(value == allowed.rstrip(".").lower() for allowed in allowed_hosts)


def _validate_url(
    url: str,
    *,
    allowed_hosts: Optional[Iterable[str]],
    allow_http: bool,
) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise WebhookPostError("webhook URL 无效") from exc
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme.lower() not in allowed_schemes:
        raise WebhookPostError("webhook URL 必须使用 HTTPS")
    if parts.username is not None or parts.password is not None:
        raise WebhookPostError("webhook URL 不允许 userinfo")
    host = parts.hostname
    if not host or not _host_allowed(host, allowed_hosts):
        raise WebhookPostError("webhook host 不在允许范围")
    if port is not None and port not in ({80, 443} if allow_http else {443}):
        raise WebhookPostError("webhook 端口不在允许范围")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        mapped = getattr(literal, "ipv4_mapped", None)
        reason = _blocked_reason(mapped or literal)
        if reason:
            raise WebhookPostError("webhook 地址不可访问")
        return
    try:
        infos = socket.getaddrinfo(
            host,
            port or (443 if parts.scheme.lower() == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise WebhookPostError("webhook host 无法解析") from exc
    if not infos:
        raise WebhookPostError("webhook host 无法解析")
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            raise WebhookPostError("webhook DNS 响应无效")
        mapped = getattr(address, "ipv4_mapped", None)
        if _blocked_reason(mapped or address):
            raise WebhookPostError("webhook 地址不可访问")


def _safe_headers(headers: Optional[Mapping[str, Any]]) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, Mapping) or len(headers) > 32:
        raise WebhookPostError("webhook headers 无效")
    out: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise WebhookPostError("webhook headers 无效")
        name = raw_name.strip()
        value = raw_value.strip()
        if (
            not name
            or any(ord(char) < 33 or ord(char) > 126 for char in name)
            or name.lower() in _BLOCKED_HEADERS
            or "\r" in value
            or "\n" in value
        ):
            raise WebhookPostError("webhook header 不允许")
        if len(name) > 128 or len(value) > 4096:
            raise WebhookPostError("webhook header 过长")
        out[name] = value
    return out


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: Optional[Mapping[str, Any]] = None,
    allowed_hosts: Optional[Iterable[str]] = None,
    allow_http: bool = False,
) -> tuple[int, Any]:
    """POST JSON 并返回 ``(status, parsed_json)``；redirect 每跳重新校验。"""
    target = (url or "").strip()
    safe_headers = _safe_headers(headers)
    safe_headers.setdefault("Content-Type", "application/json")
    try:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WebhookPostError("webhook payload 无法序列化") from exc
    if len(encoded) > 512 * 1024:
        raise WebhookPostError("webhook payload 过大")

    with httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=_TIMEOUT,
    ) as client:
        for hop in range(_MAX_REDIRECTS + 1):
            _validate_url(target, allowed_hosts=allowed_hosts, allow_http=allow_http)
            try:
                with client.stream(
                    "POST", target, content=encoded, headers=safe_headers
                ) as response:
                    if response.status_code in _REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            return response.status_code, {}
                        if hop >= _MAX_REDIRECTS:
                            raise WebhookPostError("webhook redirect 过多")
                        target = urljoin(target, location)
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            raise WebhookPostError("webhook 响应过大")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    if not raw:
                        data: Any = {}
                    else:
                        try:
                            data = json.loads(raw.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            data = {}
                    return response.status_code, data
            except WebhookPostError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                raise WebhookPostError("webhook 请求失败") from exc
    raise WebhookPostError("webhook redirect 过多")
