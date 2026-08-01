from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from coworker.connectors.wechat_ilink.models import QrCode, QrStatus
from coworker.connectors.wechat_ilink.profiles import save_confirmation
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
from coworker.server import SessionManager, create_app


class _Provider(ProviderClient):
    def complete(self, *, model, messages, tools=None, **settings):
        return AssistantTurn(text="", finish_reason="stop")

    def capabilities(self, model):
        return ModelCapabilities()


class _QrClient:
    def __init__(self):
        self.closed = False
        self.release = asyncio.Event()

    async def get_bot_qrcode(self):
        return QrCode(
            transaction="raw-poll-transaction",
            image_content="https://qr.example/scannable",
        )

    async def get_qrcode_status(self, transaction):
        await self.release.wait()
        return QrStatus(status="wait")

    async def aclose(self):
        self.closed = True
        self.release.set()


def _manager(tmp_path):
    return SessionManager(workspace=tmp_path, data_dir=tmp_path / "data", provider=_Provider())


def test_wechat_ilink_qr_api_is_authenticated_and_never_exposes_poll_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COWORKER_API_TOKEN", "t" * 64)
    manager = _manager(tmp_path)
    qr_client = _QrClient()
    manager.wechat_ilink_qr._client_factory = lambda: qr_client
    client = TestClient(create_app(manager))

    assert client.post("/v1/connectors/wechat_ilink/qrcode").status_code == 401
    headers = {"X-OpenWorker-Token": "t" * 64}
    created = client.post(
        "/v1/connectors/wechat_ilink/qrcode", json={}, headers=headers
    ).json()
    assert created["ok"] is True
    assert created["status"] == "waiting"
    assert created["qr_content"] == "https://qr.example/scannable"
    assert "raw-poll-transaction" not in repr(created)

    polled = client.get(
        f"/v1/connectors/wechat_ilink/qrcode/{created['attempt_id']}",
        headers=headers,
    ).json()
    assert polled["ok"] is True
    assert "raw-poll-transaction" not in repr(polled)
    assert client.delete(
        f"/v1/connectors/wechat_ilink/qrcode/{created['attempt_id']}",
        headers=headers,
    ).json() == {"ok": True}
    assert qr_client.closed


def test_wechat_ilink_account_api_redacts_secrets_and_scopes_allowlist(tmp_path):
    manager = _manager(tmp_path)
    save_confirmation(
        manager.secrets,
        account_id="Account-A",
        bot_token="bot-secret",
        base_url="https://ilinkai.weixin.qq.com",
        user_id="wx-internal-id",
        display_name="My WeChat",
    )
    client = TestClient(create_app(manager))

    accounts = client.get("/v1/connectors/wechat_ilink/accounts").json()
    assert accounts["ok"] is True
    assert accounts["accounts"][0]["state"] == "offline"
    assert "bot-secret" not in repr(accounts)
    assert "wx-internal-id" not in repr(accounts)
    assert "base_url" not in repr(accounts)

    allowed = client.post(
        "/v1/connectors/wechat_ilink/allow",
        json={"user_id": "friend", "team_id": "Account-A"},
    ).json()
    assert allowed["ok"] is True
    listed = client.get("/v1/connectors").json()["connectors"]
    entry = next(c for c in listed if c["name"] == "wechat_ilink")
    assert entry["accounts"][0]["allowed_users"] == ["friend"]
    assert "bot-secret" not in repr(entry)
    assert "wx-internal-id" not in repr(entry)

    deleted = client.delete(
        "/v1/connectors/wechat_ilink/accounts/Account-A"
    ).json()
    assert deleted == {"ok": True, "remaining_accounts": 0}
    assert manager.secrets.get("wechat_ilink:account:Account-A") is None


def test_wechat_ilink_reauth_requires_existing_same_account(tmp_path):
    manager = _manager(tmp_path)
    client = TestClient(create_app(manager))
    result = client.post(
        "/v1/connectors/wechat_ilink/accounts/missing/reauth"
    ).json()
    assert result == {"ok": False, "error": "account not connected"}
