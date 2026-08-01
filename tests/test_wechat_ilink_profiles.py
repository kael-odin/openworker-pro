from __future__ import annotations

import pytest

from coworker.connectors.wechat_ilink import profiles
from coworker.secrets import SecretStore


def test_profile_alias_migration_writes_canonical_shape(tmp_path):
    secrets = SecretStore(tmp_path / "secrets.json")
    secrets.put(
        "wechat_ilink:account:Bot-A",
        {
            "botToken": "secret",
            "baseUrl": "https://ilinkai.weixin.qq.com",
            "accountId": "Bot-A",
            "allowed_users": ["u1", "u1", "u2"],
            "allow_all": False,
        },
    )
    parsed = profiles.get_account(secrets, "Bot-A")
    assert parsed is not None
    assert parsed.bot_token == "secret"
    assert parsed.allowed_users == ("u1", "u2")

    profiles.save_account(secrets, parsed)
    raw = secrets.get("wechat_ilink:account:Bot-A")
    assert raw["bot_token"] == "secret"
    assert raw["base_url"] == "https://ilinkai.weixin.qq.com"
    assert raw["account_id"] == "Bot-A"
    assert not ({"botToken", "baseUrl", "accountId"} & raw.keys())


def test_confirmation_preserves_allowlist_and_default_pointer(tmp_path):
    secrets = SecretStore(tmp_path / "secrets.json")
    first = profiles.save_confirmation(
        secrets,
        account_id="Bot-A",
        bot_token="old",
        base_url="https://ilinkai.weixin.qq.com",
        user_id="wx-a",
    )
    assert profiles.default_settings(secrets)["account_id"] == "Bot-A"

    secrets.put(
        profiles.account_profile_key("Bot-A"),
        {
            **first.canonical(),
            "allowed_users": ["friend"],
            "allow_all": True,
            "needs_reauth": True,
        },
    )
    saved = profiles.save_confirmation(
        secrets,
        account_id="Bot-A",
        bot_token="new",
        base_url="https://edge.weixin.qq.com",
        user_id="wx-a",
    )
    assert saved.bot_token == "new"
    assert saved.allowed_users == ("friend",)
    assert saved.allow_all is True
    assert saved.needs_reauth is False


def test_public_rows_never_expose_credentials(tmp_path):
    secrets = SecretStore(tmp_path / "secrets.json")
    profiles.save_confirmation(
        secrets,
        account_id="Bot-A",
        bot_token="secret",
        base_url="https://ilinkai.weixin.qq.com",
        user_id="wx-a",
        display_name="My WeChat",
    )
    rows = profiles.account_rows(secrets)
    assert rows == [
        {
            "account_id": "Bot-A",
            "display_name": "My WeChat",
            "enabled": True,
            "default": True,
            "allowed_users": [],
            "allow_all": False,
            "needs_reauth": False,
        }
    ]
    assert "secret" not in repr(rows)
    assert "base_url" not in repr(rows)


def test_delete_account_repairs_default(tmp_path):
    secrets = SecretStore(tmp_path / "secrets.json")
    for account in ("A", "B"):
        profiles.save_confirmation(
            secrets,
            account_id=account,
            bot_token="token-" + account,
            base_url="https://ilinkai.weixin.qq.com",
        )
    assert profiles.delete_account(secrets, "A")
    assert profiles.default_settings(secrets)["account_id"] == "B"
    assert profiles.delete_account(secrets, "B")
    assert secrets.get(profiles.DEFAULT_PROFILE) is None


def test_account_id_is_case_preserving_and_key_safe(tmp_path):
    secrets = SecretStore(tmp_path / "secrets.json")
    profiles.save_confirmation(
        secrets,
        account_id="CaseSensitive-ID",
        bot_token="token",
        base_url="https://ilinkai.weixin.qq.com",
    )
    assert profiles.get_account(secrets, "CaseSensitive-ID") is not None
    assert profiles.get_account(secrets, "casesensitive-id") is None
    for account_id in ("a:b", "../escape", "a/b", "a\\b"):
        with pytest.raises(profiles.ProfileError):
            profiles.account_profile_key(account_id)
