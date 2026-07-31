"""企业微信自建应用连接器测试 —— 加解密 + 签名 + adapter 路由 + provider。

覆盖：
  - crypto: AES-256-CBC 加解密回路、SHA1 签名校验、PKCS#7 去填充、XML 解析。
  - provider: access_token 缓存 + 失效重取（mock httpx）、消息解析为 MessageEvent。
  - adapter: handle_callback 的 GET 验证（echostr）+ POST 消息接收 + 明文/加密模式。
  - descriptor/validate: _validate_wecom 用假凭证（mock httpx）。
  - config: wecom 在 PLATFORMS，load_settings 的 enable 判定。
"""

from __future__ import annotations

import base64
import os
import struct
import time
from unittest.mock import patch

import pytest

# -- crypto 回路 --------------------------------------------------------------


def _make_aes_key() -> str:
    """生成合法的 43 字符 EncodingAESKey（32 字节 base64 去 padding）。"""
    return base64.b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def test_aes_key_round_trip():
    """加密 → 解密回路：消息和 corpid 完整还原。"""
    from coworker.connectors.wecom_app import crypto

    key = _make_aes_key()
    corpid = "corp_test_企业"
    msg = "<xml><Content>你好企微</Content></xml>"
    enc = crypto.encrypt_message(msg, key, corpid, token="TOK", timestamp="1", nonce="N")
    plain, corp = crypto.decrypt_message(enc["encrypt"], key, corpid)
    assert plain == msg
    assert corp == corpid


def test_signature_verify():
    """SHA1 签名：正确则过，篡改则拒。"""
    from coworker.connectors.wecom_app import crypto

    token, ts, nonce, enc = "QDG6eK", "1409304348", "1372623149", "encrypted_payload"
    parts = sorted([token, ts, nonce, enc])
    import hashlib

    sig = hashlib.sha1("".join(parts).encode()).hexdigest()
    assert crypto.verify_signature(token, ts, nonce, enc, sig) is True
    assert crypto.verify_signature(token, ts, nonce, enc, "tampered") is False
    # 长度不同直接 False
    assert crypto.verify_signature(token, ts, nonce, enc, "") is False


def test_decrypt_bad_key_raises():
    """非法 EncodingAESKey 长度应报错。"""
    from coworker.connectors.wecom_app import crypto

    with pytest.raises(ValueError, match="EncodingAESKey"):
        crypto.decrypt_message("encrypt", "too_short", "corp")


def test_corpid_mismatch_raises():
    """corpid 不匹配应报错（防伪造）。"""
    from coworker.connectors.wecom_app import crypto

    key = _make_aes_key()
    enc = crypto.encrypt_message("<xml/>", key, "real_corp", token="t", timestamp="1", nonce="n")
    with pytest.raises(ValueError, match="corpid 不匹配"):
        crypto.decrypt_message(enc["encrypt"], key, "wrong_corpid")


def test_parse_callback_xml():
    """XML 解析：提取所有顶层字段。"""
    from coworker.connectors.wecom_app import crypto

    xml = (
        "<xml><ToUserName>corp1</ToUserName>"
        "<FromUserName>user1</FromUserName>"
        "<MsgType>text</MsgType><Content>hello</Content>"
        "<MsgId>1234</MsgId></xml>"
    )
    fields = crypto.parse_callback_xml(xml)
    assert fields["ToUserName"] == "corp1"
    assert fields["FromUserName"] == "user1"
    assert fields["Content"] == "hello"
    assert fields["MsgId"] == "1234"


def test_parse_xml_bytes():
    """XML 解析接受 bytes 输入。"""
    from coworker.connectors.wecom_app import crypto

    fields = crypto.parse_callback_xml(b"<xml><A>x</A></xml>")
    assert fields["A"] == "x"


# -- provider access_token 缓存 ------------------------------------------------


def test_access_token_cached_and_refreshed():
    """access_token 缓存：过期前复用，过期后重取。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    # 第一次：mock 返回 token T1, expires_in=7200
    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get:
        resp = mock_get.return_value
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"errcode": 0, "access_token": "T1", "expires_in": 7200}
        assert client._get_access_token() == "T1"
        assert client._get_access_token() == "T1"  # 第二次复用缓存
        assert mock_get.call_count == 1  # 只调一次 API

    # 强制失效后重取
    client.invalidate_token()
    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get:
        resp = mock_get.return_value
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"errcode": 0, "access_token": "T2", "expires_in": 7200}
        assert client._get_access_token() == "T2"
        assert mock_get.call_count == 1


def test_access_token_error_raises():
    """企微返回 errcode 非 0 应抛 RuntimeError。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="c", secret="bad", agent_id="1")
    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get:
        resp = mock_get.return_value
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"errcode": 40013, "errmsg": "invalid token"}
        with pytest.raises(RuntimeError, match="invalid token"):
            client._get_access_token()


def test_validate_credentials_ok():
    """validate_credentials 成功返回 ok=True。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get:
        resp = mock_get.return_value
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"errcode": 0, "access_token": "T", "expires_in": 7200}
        result = client.validate_credentials()
        assert result["ok"] is True
        assert "access_token_preview" in result


# -- provider 消息解析 --------------------------------------------------------


def test_parse_inbound_text_message():
    """文本消息解析为 MessageEvent，mentions_me=True。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient
    from coworker.connectors.base import MessageType

    client = WeComAppClient(corpid="corp1", secret="s", agent_id="1")
    xml = (
        "<xml><ToUserName>corp1</ToUserName>"
        "<FromUserName>zhangsan</FromUserName>"
        "<MsgType>text</MsgType><Content>帮我查一下数据</Content>"
        "<MsgId>123</MsgId><AgentID>1</AgentID></xml>"
    )
    event = client.parse_inbound_xml(xml)
    assert event.text == "帮我查一下数据"
    assert event.source.platform == "wecom"
    assert event.source.chat_id == "zhangsan"
    assert event.source.user_id == "zhangsan"
    assert event.source.chat_type == "dm"
    assert event.source.team_id == "corp1"
    assert event.mentions_me is True
    assert event.message_type == MessageType.TEXT


def test_parse_inbound_event_message():
    """事件消息（如关注）解析为可读描述。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    xml = (
        "<xml><FromUserName>u1</FromUserName>"
        "<MsgType>event</MsgType><Event>subscribe</Event></xml>"
    )
    event = client.parse_inbound_xml(xml)
    assert "subscribe" in event.text
    assert "u1" in event.text


def test_parse_inbound_non_text_message():
    """图片等非文本消息解析为占位描述。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    xml = "<xml><FromUserName>u1</FromUserName><MsgType>image</MsgType></xml>"
    event = client.parse_inbound_xml(xml)
    assert "image" in event.text


# -- adapter handle_callback --------------------------------------------------


@pytest.mark.asyncio
async def test_handle_callback_get_verification():
    """GET URL 验证：明文模式回 echostr，加密模式回解密后的明文。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient
    from coworker.connectors.wecom_app import crypto

    # 明文模式（无 token/aes_key）
    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    adapter = WeComAppAdapter(client)
    status, body = await adapter.handle_callback(
        {"echostr": "abc123", "msg_signature": "", "timestamp": "1", "nonce": "n"}, b""
    )
    assert status == 200
    assert body == "abc123"  # 明文模式直接回 echostr

    # 加密模式
    key = _make_aes_key()
    client2 = WeComAppClient(corpid="c", secret="s", agent_id="1", token="TOK", encoding_aes_key=key)
    adapter2 = WeComAppAdapter(client2)
    enc = crypto.encrypt_message("hello_echo", key, "c", token="TOK", timestamp="1", nonce="n")
    status, body = await adapter2.handle_callback(
        {
            "echostr": enc["encrypt"],
            "msg_signature": enc["msg_signature"],
            "timestamp": enc["timestamp"],
            "nonce": enc["nonce"],
        },
        b"",
    )
    assert status == 200
    assert body == "hello_echo"  # 解密后的明文


@pytest.mark.asyncio
async def test_handle_callback_post_message():
    """POST 消息接收：解密 → 解析 → 路由到 handler（mock）。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient
    from coworker.connectors.wecom_app import crypto

    key = _make_aes_key()
    client = WeComAppClient(corpid="c", secret="s", agent_id="1", token="TOK", encoding_aes_key=key)
    adapter = WeComAppAdapter(client)

    # 记录 handler 收到的事件
    received = []

    async def _handler(ev):
        received.append(ev)

    adapter.set_message_handler(_handler)

    # 构造加密消息 XML
    inner_xml = (
        "<xml><ToUserName>c</ToUserName>"
        "<FromUserName>zhangsan</FromUserName>"
        "<MsgType>text</MsgType><Content>你好</Content></xml>"
    )
    enc = crypto.encrypt_message(inner_xml, key, "c", token="TOK", timestamp="1", nonce="n")
    body_xml = f"<xml><Encrypt>{enc['encrypt']}</Encrypt></xml>".encode()

    status, resp = await adapter.handle_callback(
        {
            "msg_signature": enc["msg_signature"],
            "timestamp": enc["timestamp"],
            "nonce": enc["nonce"],
        },
        body_xml,
    )
    assert status == 200
    assert resp == "success"
    assert len(received) == 1
    assert received[0].text == "你好"
    assert received[0].source.platform == "wecom"


@pytest.mark.asyncio
async def test_handle_callback_bad_signature():
    """签名校验失败应返回 403。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1", token="TOK", encoding_aes_key=_make_aes_key())
    adapter = WeComAppAdapter(client)
    status, body = await adapter.handle_callback(
        {"echostr": "x", "msg_signature": "bad", "timestamp": "1", "nonce": "n"}, b""
    )
    assert status == 403
    assert "签名" in body or "失败" in body


@pytest.mark.asyncio
async def test_handle_callback_plaintext_xml():
    """明文模式 POST：XML 无 Encrypt 字段，直接当消息体解析。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")  # 无 token/aes_key
    adapter = WeComAppAdapter(client)
    received = []

    async def _handler(ev):
        received.append(ev)

    adapter.set_message_handler(_handler)
    xml = (
        "<xml><FromUserName>u1</FromUserName>"
        "<MsgType>text</MsgType><Content>明文消息</Content></xml>"
    ).encode("utf-8")
    status, resp = await adapter.handle_callback(
        {"msg_signature": "", "timestamp": "1", "nonce": "n"}, xml
    )
    assert status == 200
    assert resp == "success"
    assert len(received) == 1
    assert received[0].text == "明文消息"


# -- 出站 send ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_send_ok():
    """adapter.send 成功：mock httpx 返回 errcode=0。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    adapter = WeComAppAdapter(client)
    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get, \
         patch("coworker.connectors.wecom_app.provider.httpx.post") as mock_post:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {"errcode": 0, "access_token": "T", "expires_in": 7200}
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"errcode": 0, "msgid": "msg1"}
        result = await adapter.send("zhangsan", "回复内容")
    assert result.ok is True
    assert result.message_id == "msg1"


@pytest.mark.asyncio
async def test_adapter_send_failure():
    """adapter.send 失败：企微返回 errcode 非 0。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    adapter = WeComAppAdapter(client)
    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get, \
         patch("coworker.connectors.wecom_app.provider.httpx.post") as mock_post:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {"errcode": 0, "access_token": "T", "expires_in": 7200}
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"errcode": 40014, "errmsg": "invalid token"}
        result = await adapter.send("u", "text")
    assert result.ok is False
    assert "invalid token" in (result.error or "")


# -- descriptor + config ------------------------------------------------------


def test_wecom_descriptor_registered():
    """企微 descriptor 已注册，two_way=True，fields 含五件套。"""
    from coworker.connectors.descriptors import get_descriptor

    d = get_descriptor("wecom")
    assert d is not None
    assert d.title == "企业微信"
    assert d.two_way is True
    keys = {f.key for f in d.fields}
    assert {"corpid", "secret", "agent_id", "token", "encoding_aes_key"} <= keys


def test_wecom_in_platforms():
    """wecom 在 PLATFORMS 常量里。"""
    from coworker.connectors.config import PLATFORMS

    assert "wecom" in PLATFORMS


def test_load_settings_wecom_enabled():
    """load_settings：wecom profile 有三件套则 enabled=True。"""
    from coworker.connectors.config import load_settings
    from coworker.secrets import SecretStore

    s = SecretStore("/tmp/ow_test_wecom_settings.json")
    try:
        s.put("wecom:default", {"corpid": "c", "secret": "s", "agent_id": "1", "type": "token"})
        settings = load_settings(s)
        assert settings["wecom"].enabled is True
    finally:
        if os.path.exists(s.path):
            os.unlink(s.path)


def test_load_settings_wecom_disabled_when_missing():
    """load_settings：缺 agent_id 则 enabled=False。"""
    from coworker.connectors.config import load_settings
    from coworker.secrets import SecretStore

    s = SecretStore("/tmp/ow_test_wecom_disabled.json")
    try:
        s.put("wecom:default", {"corpid": "c", "secret": "s"})  # 缺 agent_id
        settings = load_settings(s)
        assert settings["wecom"].enabled is False
    finally:
        if os.path.exists(s.path):
            os.unlink(s.path)


def test_make_adapter_wecom():
    """make_adapter：合法 profile 返回 WeComAppAdapter，缺字段返回 None。"""
    from coworker.connectors.adapters import make_adapter

    prof = {"corpid": "c", "secret": "s", "agent_id": "1", "token": "t", "encoding_aes_key": "k" * 43}
    adapter = make_adapter("wecom", prof)
    assert adapter is not None
    assert adapter.platform == "wecom"
    assert adapter.client.agent_id == "1"
    # 缺 agent_id
    assert make_adapter("wecom", {"corpid": "c", "secret": "s"}) is None


def test_validate_wecom_ok():
    """_validate_wecom：mock httpx 返回成功 → ok=True。"""
    from coworker.connectors.descriptors import _validate_wecom

    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {"errcode": 0, "access_token": "T", "expires_in": 7200}
        result = _validate_wecom({"corpid": "c", "secret": "s", "agent_id": "1"})
    assert result.ok is True
    assert "1" in (result.identity or "")


def test_validate_wecom_missing_fields():
    """_validate_wecom：缺字段 → ok=False。"""
    from coworker.connectors.descriptors import _validate_wecom

    result = _validate_wecom({"corpid": "", "secret": "", "agent_id": ""})
    assert result.ok is False


# -- sender -------------------------------------------------------------------


def test_send_wecom_parses_profile_json():
    """_send_wecom：token 是 profile JSON 串，解析后构造 client 发送（mock httpx）。"""
    from coworker.connectors.senders import _send_wecom
    import json

    profile = json.dumps({"corpid": "c", "secret": "s", "agent_id": "1"})
    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get, \
         patch("coworker.connectors.wecom_app.provider.httpx.post") as mock_post:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {"errcode": 0, "access_token": "T", "expires_in": 7200}
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"errcode": 0, "msgid": "m1"}
        result = _send_wecom(profile, "zhangsan", "你好")
    assert result.ok is True
    assert result.message_id == "m1"


def test_send_wecom_bad_token():
    """_send_wecom：token 非法 JSON → 失败。"""
    from coworker.connectors.senders import _send_wecom

    result = _send_wecom("not_json", "u", "text")
    assert result.ok is False
    assert "凭证" in (result.error or "")
