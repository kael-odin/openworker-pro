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
import threading
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


def test_pkcs7_unpad_rejects_invalid_padding():
    """非法/空 PKCS#7 填充必须 fail closed。"""
    from coworker.connectors.wecom_app import crypto

    for value in (b"", b"plain\x00", b"plain\x21", b"plain\x02\x03"):
        with pytest.raises(ValueError, match="PKCS#7"):
            crypto._pkcs7_unpad(value)


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


def test_decrypt_rejects_bad_ciphertext_padding():
    """合法 block 长度但解密后 padding 被篡改时必须拒绝。"""
    from coworker.connectors.wecom_app import crypto

    key = _make_aes_key()
    enc = crypto.encrypt_message("<xml/>", key, "corp")
    raw = bytearray(base64.b64decode(enc["encrypt"]))
    raw[-1] ^= 0x01
    tampered = base64.b64encode(raw).decode("ascii")
    with pytest.raises(ValueError, match="PKCS#7"):
        crypto.decrypt_message(tampered, key, "corp")


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


def test_access_token_concurrent_refresh_is_single_flight():
    """多个 worker 同时遇到过期 token 时只刷新一次。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    barrier = threading.Barrier(5)
    values = []

    def fetch():
        barrier.wait()
        values.append(client._get_access_token())

    with patch("coworker.connectors.wecom_app.provider.httpx.get") as mock_get:
        response = mock_get.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = {"errcode": 0, "access_token": "ONE", "expires_in": 7200}
        threads = [threading.Thread(target=fetch) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert values == ["ONE"] * 5
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


def test_parse_template_card_event_button_list():
    """template_card 按钮点击回调解析为 InteractionEvent，value = 按钮的 encoded key，
    message_id = 卡片 task_id（update_taskcard 用它定位卡片）。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="corp1", secret="s", agent_id="1")
    import json as _json

    encoded = _json.dumps({"id": "item-abc", "r": "allow"})
    xml = (
        "<xml><ToUserName>corp1</ToUserName>"
        "<FromUserName>zhangsan</FromUserName>"
        "<MsgType>event</MsgType><Event>template_card_event</Event>"
        f"<TaskId>ow_item-abc</TaskId><ButtonKeys>{encoded}</ButtonKeys></xml>"
    )
    ev = client.parse_template_card_event(xml)
    assert ev is not None
    assert ev.platform == "wecom"
    assert ev.value == encoded
    assert ev.chat_id == "zhangsan"
    assert ev.message_id == "ow_item-abc"  # task_id for update_taskcard
    assert ev.user_id == "zhangsan"
    assert ev.team_id == "corp1"


def test_parse_template_card_event_not_a_card_click():
    """普通事件（如 subscribe）不是卡片点击，返回 None —— 走普通消息路径。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    xml = (
        "<xml><FromUserName>u1</FromUserName>"
        "<MsgType>event</MsgType><Event>subscribe</Event></xml>"
    )
    assert client.parse_template_card_event(xml) is None


def test_parse_template_card_event_selected_items_fallback():
    """SelectedItems（JSON 含 SelectedKey）也能取出 value —— 覆盖非 button_list 卡片变体。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient

    client = WeComAppClient(corpid="corp1", secret="s", agent_id="1")
    import json as _json

    encoded = _json.dumps({"id": "item-x", "r": "deny"})
    sel = _json.dumps([{"SelectedKey": encoded}])
    xml = (
        "<xml><FromUserName>u1</FromUserName>"
        "<MsgType>event</MsgType><Event>template_card_event</Event>"
        f"<TaskId>t1</TaskId><SelectedItems>{sel}</SelectedItems></xml>"
    )
    ev = client.parse_template_card_event(xml)
    assert ev is not None and ev.value == encoded


def test_send_template_card_payload_shape(monkeypatch):
    """send_template_card 发的 payload 是 button_list 卡片，每个 button 的 key = encoded value。"""
    from coworker.connectors.wecom_app.provider import WeComAppClient
    from coworker.interactions import Button, encode

    client = WeComAppClient(corpid="c", secret="s", agent_id="100")
    # stub access_token + httpx.post to capture the payload
    monkeypatch.setattr(client, "_get_access_token", lambda: "tok")
    captured: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"errcode": 0, "msgid": "m1"}

        def raise_for_status(self):
            pass

    import coworker.connectors.wecom_app.provider as prov

    monkeypatch.setattr(prov.httpx, "post", lambda *a, **k: (captured.append(k["json"]), _Resp())[1])

    btns = [Button("批准", encode("item1", "allow")), Button("拒绝", encode("item1", "deny"))]
    data = client.send_template_card("zhangsan", "审批", "运行 write_file？", btns)
    assert data["errcode"] == 0
    payload = captured[0]
    assert payload["msgtype"] == "template_card"
    card = payload["template_card"]
    assert card["card_type"] == "button_list"
    assert card["task_id"].startswith("ow_item1")
    keys = [b["key"] for b in card["button_list"]]
    assert keys == [encode("item1", "allow"), encode("item1", "deny")]


# -- adapter handle_callback --------------------------------------------------


@pytest.mark.asyncio
async def test_handle_callback_get_verification():
    """GET URL 验证：明文模式回 echostr，加密模式回解密后的明文。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient
    from coworker.connectors.wecom_app import crypto

    # 未配置回调凭据时保留出站，但公网 GET 回调 fail closed。
    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
    adapter = WeComAppAdapter(client)
    status, body = await adapter.handle_callback(
        {"echostr": "abc123", "msg_signature": "", "timestamp": str(int(time.time())), "nonce": "n"}, b""
    )
    assert status == 403
    assert body == "invalid callback"

    # 加密模式
    key = _make_aes_key()
    client2 = WeComAppClient(corpid="c", secret="s", agent_id="1", token="TOK", encoding_aes_key=key)
    adapter2 = WeComAppAdapter(client2)
    now = str(int(time.time()))
    enc = crypto.encrypt_message("hello_echo", key, "c", token="TOK", timestamp=now, nonce="n")
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
    now = str(int(time.time()))
    enc = crypto.encrypt_message(inner_xml, key, "c", token="TOK", timestamp=now, nonce="n")
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
        {"echostr": "x", "msg_signature": "bad", "timestamp": str(int(time.time())), "nonce": "n"}, b""
    )
    assert status == 403
    assert body == "invalid callback"


@pytest.mark.asyncio
async def test_handle_callback_plaintext_xml():
    """没有回调密钥的旧 profile 保留出站，但拒绝明文入站。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient

    client = WeComAppClient(corpid="c", secret="s", agent_id="1")
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
        {"msg_signature": "", "timestamp": str(int(time.time())), "nonce": "n"}, xml
    )
    assert status == 403
    assert resp == "invalid callback"
    assert received == []


@pytest.mark.asyncio
async def test_handle_callback_rejects_plaintext_downgrade():
    """已配置加密回调时，无 Encrypt 的 POST 也不得降级为明文。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient

    client = WeComAppClient(
        corpid="c", secret="s", agent_id="1", token="TOK", encoding_aes_key=_make_aes_key()
    )
    adapter = WeComAppAdapter(client)
    xml = b"<xml><FromUserName>u1</FromUserName><MsgType>text</MsgType></xml>"
    status, body = await adapter.handle_callback(
        {
            "msg_signature": "not-used",
            "timestamp": str(int(time.time())),
            "nonce": "n",
        },
        xml,
    )
    assert status == 403
    assert body == "invalid callback"


@pytest.mark.asyncio
async def test_handle_callback_rejects_stale_timestamp():
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient, crypto

    key = _make_aes_key()
    client = WeComAppClient(
        corpid="c", secret="s", agent_id="1", token="TOK", encoding_aes_key=key
    )
    adapter = WeComAppAdapter(client)
    enc = crypto.encrypt_message(
        "hello", key, "c", token="TOK", timestamp="1", nonce="n"
    )
    status, body = await adapter.handle_callback(
        {
            "echostr": enc["encrypt"],
            "msg_signature": enc["msg_signature"],
            "timestamp": "1",
            "nonce": "n",
        },
        b"",
    )
    assert status == 403
    assert body == "invalid callback"


@pytest.mark.asyncio
async def test_handle_callback_replay_and_message_id_dedup():
    """同一签名重放及不同签名下的重复 MsgId 都不得二次分发。"""
    from coworker.connectors.wecom_app import WeComAppAdapter, WeComAppClient, crypto

    key = _make_aes_key()
    client = WeComAppClient(
        corpid="c", secret="s", agent_id="1", token="TOK", encoding_aes_key=key
    )
    adapter = WeComAppAdapter(client)
    received = []

    async def _handler(event):
        received.append(event)

    adapter.set_message_handler(_handler)
    inner = (
        "<xml><FromUserName>u1</FromUserName><MsgType>text</MsgType>"
        "<Content>x</Content><MsgId>same-id</MsgId></xml>"
    )
    now = str(int(time.time()))
    first = crypto.encrypt_message(inner, key, "c", token="TOK", timestamp=now, nonce="n1")
    first_body = f"<xml><Encrypt>{first['encrypt']}</Encrypt></xml>".encode()
    first_query = {
        "msg_signature": first["msg_signature"],
        "timestamp": now,
        "nonce": "n1",
    }
    assert await adapter.handle_callback(first_query, first_body) == (200, "success")
    assert await adapter.handle_callback(first_query, first_body) == (200, "success")

    second = crypto.encrypt_message(inner, key, "c", token="TOK", timestamp=now, nonce="n2")
    second_body = f"<xml><Encrypt>{second['encrypt']}</Encrypt></xml>".encode()
    second_query = {
        "msg_signature": second["msg_signature"],
        "timestamp": now,
        "nonce": "n2",
    }
    assert await adapter.handle_callback(second_query, second_body) == (200, "success")
    assert len(received) == 1


def test_callback_credentials_must_be_paired():
    from coworker.connectors.wecom_app import WeComAppClient
    from coworker.connectors.wecom_app.provider import client_from_profile

    with pytest.raises(ValueError, match="必须同时配置"):
        WeComAppClient(corpid="c", secret="s", agent_id="1", token="TOK")
    client = client_from_profile(
        {"corpid": "c", "secret": "s", "agent_id": "1", "token": "legacy-only"}
    )
    assert client is not None
    assert client.inbound_state == "outbound_only"


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
    """_validate_wecom：缺字段或回调密钥只填一个 → ok=False。"""
    from coworker.connectors.descriptors import _validate_wecom

    result = _validate_wecom({"corpid": "", "secret": "", "agent_id": ""})
    assert result.ok is False
    pair = _validate_wecom(
        {"corpid": "c", "secret": "s", "agent_id": "1", "token": "only-token"}
    )
    assert pair.ok is False
    assert "必须同时配置" in (pair.error or "")


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
