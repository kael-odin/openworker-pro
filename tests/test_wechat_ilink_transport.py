from __future__ import annotations

import base64
import ipaddress

import httpx
import pytest

from coworker.connectors.wechat_ilink.client import IlinkClient
from coworker.connectors.wechat_ilink.transport import (
    DEFAULT_BASE_URL,
    IlinkTransport,
    IlinkTransportError,
    auth_headers,
    validate_base_url,
)


def _resolver_for(*addresses: str):
    def resolve(_host, port, **_kwargs):
        return [
            (2 if ipaddress.ip_address(address).version == 4 else 23, 1, 6, "", (address, port))
            for address in addresses
        ]

    return resolve


def test_validate_base_url_requires_vendor_https_origin():
    public = _resolver_for("203.0.113.9")
    # TEST-NET is reserved and must be rejected; use a routable documentation stand-in
    # accepted by the injected resolver only after choosing a globally classified address.
    public = _resolver_for("1.1.1.1")
    assert (
        validate_base_url("https://ilinkai.weixin.qq.com/", resolver=public)
        == DEFAULT_BASE_URL
    )
    assert (
        validate_base_url("https://edge.weixin.qq.com", resolver=public)
        == "https://edge.weixin.qq.com"
    )
    for invalid in (
        "http://ilinkai.weixin.qq.com",
        "https://user@ilinkai.weixin.qq.com",
        "https://ilinkai.weixin.qq.com:8443",
        "https://ilinkai.weixin.qq.com/prefix",
        "https://ilinkai.weixin.qq.com.evil.example",
        "https://127.0.0.1",
    ):
        with pytest.raises(IlinkTransportError, match="invalid_base_url"):
            validate_base_url(invalid, resolver=public)


def test_validate_base_url_rejects_any_private_dns_answer():
    with pytest.raises(IlinkTransportError, match="base_url_blocked"):
        validate_base_url(
            DEFAULT_BASE_URL, resolver=_resolver_for("1.1.1.1", "127.0.0.1")
        )


def test_auth_headers_use_uint32_decimal_base64(monkeypatch):
    monkeypatch.setattr("secrets.randbits", lambda bits: 4294967295)
    headers = auth_headers("secret-token")
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert base64.b64decode(headers["X-WECHAT-UIN"]).decode() == "4294967295"


async def test_transport_rejects_redirect_without_following():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secret"})

    transport = IlinkTransport(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=_resolver_for("1.1.1.1"),
    )
    with pytest.raises(IlinkTransportError, match="redirect_refused"):
        await transport.get("/ilink/bot/get_bot_qrcode")
    assert len(seen) == 1


async def test_transport_bounds_and_redacts_response():
    secret_body = b'{"bot_token":"do-not-reflect"}'

    transport = IlinkTransport(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=secret_body)
            )
        ),
        resolver=_resolver_for("1.1.1.1"),
        max_response_bytes=8,
    )
    with pytest.raises(IlinkTransportError) as exc:
        await transport.get("/ilink/bot/get_bot_qrcode")
    assert exc.value.code == "response_too_large"
    assert "do-not-reflect" not in str(exc.value)


async def test_client_builds_observed_protocol_requests():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("get_bot_qrcode"):
            return httpx.Response(200, json={"qrcode": "tx", "qrcode_img_content": "qr"})
        if request.url.path.endswith("get_qrcode_status"):
            return httpx.Response(200, json={"status": "wait"})
        if request.url.path.endswith("getupdates"):
            return httpx.Response(200, json={"ret": 0, "get_updates_buf": "next"})
        return httpx.Response(200, json={"ret": 0})

    transport = IlinkTransport(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=_resolver_for("1.1.1.1"),
    )
    client = IlinkClient(transport)
    await client.get_bot_qrcode()
    await client.get_qrcode_status("tx")
    updates = await client.get_updates("token", "old")
    sent = await client.send_text(
        bot_token="token",
        to_user_id="u1",
        text="hello",
        context_token="context",
        client_id="fixed-id",
    )

    assert updates.cursor == "next" and sent.ok
    assert requests[0].url.params["bot_type"] == "3"
    assert requests[1].url.params["qrcode"] == "tx"
    assert requests[1].headers["ilink-app-clientversion"] == "1"
    updates_body = __import__("json").loads(requests[2].content)
    assert updates_body == {
        "get_updates_buf": "old",
        "base_info": {"channel_version": "1.0.2"},
    }
    send_body = __import__("json").loads(requests[3].content)
    assert send_body["msg"]["message_type"] == 2
    assert send_body["msg"]["message_state"] == 2
    assert send_body["msg"]["client_id"] == "fixed-id"
    assert send_body["msg"]["context_token"] == "context"
