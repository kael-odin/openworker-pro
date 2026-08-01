"""notify 包测试 —— 渠道 payload、router 分级、config 脱敏、dispatch 链路。

无网络：各 channel 在函数内 `import httpx` 后调 `httpx.post`，故 monkeypatch 全局
``httpx.post`` 即可拦截。捕获 payload 并返回固定响应。
"""

from __future__ import annotations

import base64
import json
import socket
from typing import Any

import httpx
import pytest

from coworker.notify import NotifyRouter, NotifyConfigStore, CHANNEL_TYPES
from coworker.notify.channels import dingtalk, feishu, wecom_webhook, generic_webhook
from coworker.notify.http import WebhookPostError, post_json
from coworker.notify.router import DEFAULT_LEVEL
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient


# -- fake httpx.post ----------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


@pytest.fixture
def captured(monkeypatch):
    """替换渠道导入的受限 post_json，捕获 (url, json) 并返回固定响应。"""
    calls: list[tuple[str, dict[str, Any]]] = []
    current: list[FakeResponse] = [FakeResponse()]

    def _post(url, payload, **kwargs):
        calls.append((url, payload or {}))
        response = current[0]
        return response.status_code, response._payload

    for module in (dingtalk, feishu, wecom_webhook, generic_webhook):
        monkeypatch.setattr(module, "post_json", _post)

    def make_fake(response: FakeResponse):
        current[0] = response

    return calls, make_fake


# -- channel payload ----------------------------------------------------------
def test_dingtalk_signs_when_secret_given(captured):
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 0}))
    cfg = {"url": "https://oapi.dingtalk.com/robot/send?access_token=X", "secret": "SECsecret"}
    r = dingtalk.send("标题", "正文", cfg, status="ok")
    assert r.ok and r.channel == "dingtalk"
    url, payload = calls[0]
    assert "timestamp=" in url and "sign=" in url  # 加签参数追加到 URL
    assert payload["msgtype"] == "markdown"
    assert "标题" in payload["markdown"]["title"]
    assert "正文" in payload["markdown"]["text"]
    assert "ok" in payload["markdown"]["text"]  # status 注入正文


def test_dingtalk_no_secret_no_sign(captured):
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 0}))
    cfg = {"url": "https://oapi.dingtalk.com/robot/send?access_token=X"}
    dingtalk.send("t", "b", cfg)
    url, _ = calls[0]
    assert "sign=" not in url  # 无 secret 不加签


def test_dingtalk_errcode_is_failure(captured):
    _, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 300001, "errmsg": "token is invalid"}))
    r = dingtalk.send("t", "b", {"url": "https://x"})
    assert not r.ok
    assert "300001" in (r.error or "")


def test_feishu_sign_hex_upper(captured):
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"code": 0}))
    cfg = {"url": "https://open.feishu.cn/hook/x", "secret": "abc"}
    feishu.send("标题", "正文", cfg)
    _, payload = calls[0]
    assert "timestamp" in payload and "sign" in payload
    # 签名是十六进制大写
    assert payload["sign"] == payload["sign"].upper()
    assert all(c in "0123456789ABCDEF" for c in payload["sign"])


def test_feishu_code_nonzero_is_failure(captured):
    _, make_fake = captured
    make_fake(FakeResponse(payload={"code": 19021, "msg": "bad sign"}))
    r = feishu.send("t", "b", {"url": "https://x"})
    assert not r.ok
    assert "19021" in (r.error or "")


def test_wecom_sign_base64(captured):
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 0}))
    cfg = {"url": "https://qyapi.weixin.qq.com/webhook?key=X", "secret": "abc"}
    wecom_webhook.send("标题", "正文", cfg)
    _, payload = calls[0]
    assert "timestamp" in payload and "sign" in payload
    # 企微签名是 base64（可解码回 bytes）
    base64.b64decode(payload["sign"])


def test_generic_webhook_posts_json(captured):
    calls, make_fake = captured
    make_fake(FakeResponse(200))
    cfg = {"url": "https://example.com/hook"}
    r = generic_webhook.send("标题", "正文", cfg, status="error")
    assert r.ok
    _, payload = calls[0]
    assert payload == {"title": "标题", "body": "正文", "status": "error"}


def test_generic_webhook_missing_url():
    r = generic_webhook.send("t", "b", {})
    assert not r.ok and "url" in (r.error or "")


def test_http_error_is_failure(captured):
    _, make_fake = captured
    make_fake(FakeResponse(500, {"e": "x"}))
    r = generic_webhook.send("t", "b", {"url": "https://x"})
    assert not r.ok
    assert "500" in (r.error or "")
    assert "x" not in (r.error or "")


def _public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]


def test_safe_webhook_rejects_private_userinfo_and_bad_headers(monkeypatch):
    with pytest.raises(WebhookPostError, match="地址不可访问"):
        post_json("https://127.0.0.1/hook", {"x": 1})
    with pytest.raises(WebhookPostError, match="userinfo"):
        post_json("https://user:pass@example.com/hook", {"x": 1})
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    with pytest.raises(WebhookPostError, match="header"):
        post_json("https://example.com/hook", {"x": 1}, headers={"Host": "evil"})


def test_safe_webhook_rejects_dns_with_any_private_answer(monkeypatch):
    def mixed(host, port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    with pytest.raises(WebhookPostError, match="地址不可访问"):
        post_json("https://example.com/hook", {"x": 1})


def test_safe_webhook_revalidates_redirect(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"}, request=request)

    original = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("coworker.notify.http.httpx.Client", client_factory)
    with pytest.raises(WebhookPostError, match="地址不可访问"):
        post_json("https://example.com/hook", {"x": 1})


def test_vendor_webhook_requires_exact_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    result = dingtalk.send(
        "t", "b", {"url": "https://attacker.example/robot/send?access_token=secret"}
    )
    assert not result.ok
    assert "host" in (result.error or "")
    assert "secret" not in (result.error or "")


# -- config store -------------------------------------------------------------
def test_config_roundtrip_and_mask(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    store = NotifyConfigStore()
    assert store.get_config("dingtalk") == {}
    store.save_config("dingtalk", {"enabled": True, "url": "https://x", "secret": "SECabc"})
    got = store.get_config("dingtalk")
    assert got["url"] == "https://x"
    assert got["secret"] == "SECabc"
    # 脱敏
    masked = NotifyConfigStore.mask(got)
    assert masked["url"] == "••••••"
    assert masked["secret"] == "••••••"
    # list_channels 报告 configured/enabled
    infos = {c["channel"]: c for c in store.list_channels()}
    assert infos["dingtalk"]["configured"] is True
    assert infos["dingtalk"]["enabled"] is True
    assert infos["feishu"]["configured"] is False
    # delete
    assert store.delete_config("dingtalk") is True
    assert store.get_config("dingtalk") == {}


def test_config_patch_preserves_masks_and_supports_explicit_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    store = NotifyConfigStore()
    store.save_config(
        "dingtalk",
        {"enabled": True, "url": "https://secret.example/hook?k=1", "secret": "SEC-old"},
    )
    store.save_config("dingtalk", {"secret": "••••••"})
    got = store.get_config("dingtalk")
    assert got["url"].endswith("k=1")
    assert got["secret"] == "SEC-old"
    assert got["enabled"] is True

    store.set_enabled("dingtalk", False)
    got = store.get_config("dingtalk")
    assert got["url"].endswith("k=1")
    assert got["secret"] == "SEC-old"
    assert got["enabled"] is False

    store.save_config("dingtalk", {}, clear_fields=["secret"])
    assert "secret" not in store.get_config("dingtalk")
    with pytest.raises(ValueError, match="clear_fields"):
        store.save_config("dingtalk", {}, clear_fields=["not_allowed"])


def test_test_config_merges_server_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    store = NotifyConfigStore()
    store.save_config("dingtalk", {"url": "https://stored", "secret": "stored-secret"})
    merged = store.merge_for_test("dingtalk", {"url": "••••••", "secret": "******"})
    assert merged["url"] == "https://stored"
    assert merged["secret"] == "stored-secret"


# -- router level filtering ---------------------------------------------------
def _store_with_dingtalk(tmp_path):
    s = NotifyConfigStore()
    s.save_config("dingtalk", {"enabled": True, "url": "https://x"})
    return s


def test_router_important_skips_ok(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 0}))
    router = NotifyRouter(_store_with_dingtalk(tmp_path))
    # ok + important → 不发
    d = router.dispatch_run(task_name="T", run_status="ok", result_text="done", level="important")
    assert d.results == []
    assert calls == []


def test_router_important_sends_error(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 0}))
    router = NotifyRouter(_store_with_dingtalk(tmp_path))
    d = router.dispatch_run(task_name="T", run_status="error", result_text="", error="boom", level="important")
    assert d.any_ok
    assert len(calls) == 1
    _, payload = calls[0]
    assert "失败" in payload["markdown"]["title"]
    assert "boom" in payload["markdown"]["text"]


def test_router_none_sends_never(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 0}))
    router = NotifyRouter(_store_with_dingtalk(tmp_path))
    d = router.dispatch_run(task_name="T", run_status="error", result_text="", error="x", level="none")
    assert d.results == []
    assert calls == []


def test_router_all_sends_ok(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 0}))
    router = NotifyRouter(_store_with_dingtalk(tmp_path))
    d = router.dispatch_run(task_name="T", run_status="ok", result_text="done", level="all")
    assert d.any_ok
    assert len(calls) == 1


def test_router_dispatch_respects_enabled_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    s = NotifyConfigStore()
    s.save_config("dingtalk", {"enabled": False, "url": "https://x"})  # 配置了但未启用
    router = NotifyRouter(s)
    d = router.dispatch(title="t", body="b", status="error", level="all")
    assert d.results == []  # 未启用渠道被跳过


def test_router_test_send_uses_injected_config(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    calls, make_fake = captured
    make_fake(FakeResponse(payload={"errcode": 0}))
    router = NotifyRouter()
    d = router.test_send("dingtalk", {"url": "https://x"})
    assert d.any_ok
    _, payload = calls[0]
    # 测试标题固定
    assert "测试" in payload["markdown"]["title"]


def test_router_unknown_channel():
    router = NotifyRouter()
    d = router.dispatch(title="t", body="b", status="error", channels=["nope"], level="all")
    assert not d.any_ok
    assert "未知渠道" in (d.results[0].error or "")


def test_default_level_is_important():
    assert DEFAULT_LEVEL == "important"


def test_channel_types_complete():
    assert set(CHANNEL_TYPES) == {"dingtalk", "feishu", "wecom", "webhook", "email"}


class _Provider(ProviderClient):
    def complete(self, *, model, messages, tools=None, **settings):
        return AssistantTurn(text="")

    def capabilities(self, model):
        return ModelCapabilities()


def test_notify_api_patch_toggle_and_masked_test(tmp_path, monkeypatch):
    """HTTP API 的 PATCH/toggle/test 都由服务端保留已存 secret。"""
    from fastapi.testclient import TestClient
    from coworker.server import SessionManager, create_app

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    manager = SessionManager(workspace=tmp_path, provider=_Provider())
    store = manager.notify_router.store
    store.save_config(
        "dingtalk",
        {"enabled": True, "url": "https://stored.example/hook", "secret": "stored"},
    )
    client = TestClient(create_app(manager))

    masked = client.get("/v1/notify/channels/dingtalk").json()["config"]
    assert masked["url"] == "••••••" and masked["secret"] == "••••••"
    assert client.patch(
        "/v1/notify/channels/dingtalk", json={"enabled": False}
    ).json()["ok"]
    assert store.get_config("dingtalk")["url"] == "https://stored.example/hook"

    assert client.patch(
        "/v1/notify/channels/dingtalk",
        json={"config": {"url": "••••••", "secret": "••••••"}},
    ).json()["ok"]
    assert store.get_config("dingtalk")["secret"] == "stored"

    seen = {}

    def fake_test(channel, config):
        seen.update(store.merge_for_test(channel, config))
        from coworker.notify.router import NotifyDispatch
        return NotifyDispatch()

    monkeypatch.setattr(manager.notify_router, "test_send", fake_test)
    response = client.post(
        "/v1/notify/test",
        json={"channel": "dingtalk", "config": {"secret": "••••••"}},
    )
    assert response.status_code == 200
    assert seen["secret"] == "stored"
    assert "stored" not in response.text
