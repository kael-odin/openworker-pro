from __future__ import annotations

import asyncio

import pytest

from coworker.connectors.wechat_ilink.auth import QrAttemptRegistry
from coworker.connectors.wechat_ilink.models import QrCode, QrStatus
from coworker.connectors.wechat_ilink.transport import IlinkTransportError
from coworker.secrets import SecretStore


class FakeQrClient:
    def __init__(self, statuses, *, qr_content="https://qr.example/display"):
        self.statuses = list(statuses)
        self.qr_content = qr_content
        self.active_calls = 0
        self.max_active_calls = 0
        self.closed = False
        self.transactions: list[str] = []

    async def get_bot_qrcode(self):
        return QrCode(transaction="raw-poll-secret", image_content=self.qr_content)

    async def get_qrcode_status(self, transaction):
        self.transactions.append(transaction)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        await asyncio.sleep(0)
        self.active_calls -= 1
        if self.statuses:
            return self.statuses.pop(0)
        return QrStatus(status="wait")

    async def aclose(self):
        self.closed = True


async def _wait_terminal(registry, attempt_id, *, tries=100):
    for _ in range(tries):
        value = await registry.get(attempt_id)
        if value and value["status"] in {"confirmed", "expired", "failed", "cancelled"}:
            return value
        await asyncio.sleep(0.002)
    raise AssertionError("attempt did not finish")


async def test_qr_registry_confirms_and_keeps_secrets_backend_only(tmp_path, monkeypatch):
    # The confirmation's vendor baseurl is revalidated before it reaches SecretStore.
    monkeypatch.setattr(
        "coworker.connectors.wechat_ilink.auth.validate_base_url",
        lambda url: url.rstrip("/"),
    )
    client = FakeQrClient(
        [
            QrStatus(status="wait"),
            QrStatus(status="scaned"),
            QrStatus(
                status="confirmed",
                bot_token="bot-secret",
                account_id="account-A",
                base_url="https://ilinkai.weixin.qq.com/",
                user_id="wx-user",
            ),
        ]
    )
    confirmed: list[str] = []
    secrets = SecretStore(tmp_path / "secrets.json")
    registry = QrAttemptRegistry(
        secrets,
        client_factory=lambda: client,
        on_confirm=confirmed.append,
        poll_interval=0.001,
    )

    created = await registry.create()
    assert created["status"] == "waiting"
    assert created["qr_content"] == "https://qr.example/display"
    assert "raw-poll-secret" not in repr(created)
    final = await _wait_terminal(registry, created["attempt_id"])

    assert final["status"] == "confirmed"
    assert final["account_id"] == "account-A"
    assert "bot-secret" not in repr(final)
    assert "raw-poll-secret" not in repr(final)
    profile = secrets.get("wechat_ilink:account:account-A")
    assert profile["bot_token"] == "bot-secret"
    assert confirmed == ["account-A"]
    assert client.max_active_calls == 1
    assert set(client.transactions) == {"raw-poll-secret"}
    assert client.closed


async def test_qr_registry_rejects_missing_display_content_without_exposing_transaction(
    tmp_path,
):
    client = FakeQrClient([], qr_content="")
    registry = QrAttemptRegistry(
        SecretStore(tmp_path / "secrets.json"), client_factory=lambda: client
    )

    with pytest.raises(IlinkTransportError, match="qrcode_image_unavailable"):
        await registry.create()

    assert client.closed
    assert registry._attempts == {}


async def test_qr_registry_reauth_rejects_different_account(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coworker.connectors.wechat_ilink.auth.validate_base_url", lambda url: url
    )
    client = FakeQrClient(
        [
            QrStatus(
                status="confirmed",
                bot_token="secret",
                account_id="other-account",
                base_url="https://ilinkai.weixin.qq.com",
            )
        ]
    )
    secrets = SecretStore(tmp_path / "secrets.json")
    registry = QrAttemptRegistry(
        secrets, client_factory=lambda: client, poll_interval=0.001
    )
    created = await registry.create(reauth_account_id="expected-account")
    final = await _wait_terminal(registry, created["attempt_id"])
    assert final["status"] == "failed"
    assert final["error"] == "reauth_account_mismatch"
    assert secrets.get("wechat_ilink:account:other-account") is None


async def test_qr_registry_cancel_awaits_worker_and_redacts_qr(tmp_path):
    client = FakeQrClient([])
    registry = QrAttemptRegistry(
        SecretStore(tmp_path / "secrets.json"),
        client_factory=lambda: client,
        poll_interval=60,
    )
    created = await registry.create()
    assert await registry.cancel(created["attempt_id"])
    final = await registry.get(created["attempt_id"])
    assert final["status"] == "cancelled"
    assert "qr_content" not in final
    assert client.closed


async def test_qr_registry_expires_attempt(tmp_path):
    client = FakeQrClient([])
    registry = QrAttemptRegistry(
        SecretStore(tmp_path / "secrets.json"),
        client_factory=lambda: client,
        ttl_seconds=0.005,
        poll_interval=0.001,
    )
    created = await registry.create()
    final = await _wait_terminal(registry, created["attempt_id"])
    assert final["status"] == "expired"
    assert client.closed
