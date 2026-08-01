"""SecretStore profiles for WeChat iLink's multi-account connector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional

from ...secrets import SecretStore

PROFILE_PREFIX = "wechat_ilink:account:"
DEFAULT_PROFILE = "wechat_ilink:default"
MAX_ACCOUNT_ID_CHARS = 512
MAX_DISPLAY_NAME_CHARS = 512
MAX_ALLOWED_USERS = 10_000


class ProfileError(ValueError):
    pass


def _text(value: Any, *, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ProfileError("invalid profile field")
    value = value.strip()
    if (required and not value) or len(value) > maximum or "\x00" in value:
        raise ProfileError("invalid profile field")
    return value


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set)) or len(value) > MAX_ALLOWED_USERS:
        raise ProfileError("invalid allowed users")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        user_id = _text(item, maximum=MAX_ACCOUNT_ID_CHARS, required=True)
        if user_id not in seen:
            out.append(user_id)
            seen.add(user_id)
    return tuple(out)


def account_profile_key(account_id: str) -> str:
    safe = _text(account_id, maximum=MAX_ACCOUNT_ID_CHARS, required=True)
    if safe in (".", "..") or any(ch in safe for ch in (":", "/", "\\", "\r", "\n")):
        raise ProfileError("invalid account id")
    return PROFILE_PREFIX + safe


@dataclass(frozen=True)
class AccountProfile:
    account_id: str
    bot_token: str
    base_url: str
    user_id: str = ""
    display_name: str = ""
    enabled: bool = True
    allowed_users: tuple[str, ...] = ()
    allow_all: bool = False
    needs_reauth: bool = False

    @classmethod
    def parse(cls, value: Mapping[str, Any], *, fallback_id: str = "") -> "AccountProfile":
        if not isinstance(value, Mapping):
            raise ProfileError("invalid account profile")
        account_id = value.get("account_id", value.get("accountId", fallback_id))
        bot_token = value.get("bot_token", value.get("botToken", ""))
        base_url = value.get("base_url", value.get("baseUrl", ""))
        return cls(
            account_id=_text(
                account_id, maximum=MAX_ACCOUNT_ID_CHARS, required=True
            ),
            bot_token=_text(bot_token, maximum=16 * 1024),
            base_url=_text(base_url, maximum=2 * 1024, required=True),
            user_id=_text(
                value.get("user_id", value.get("userId", "")),
                maximum=MAX_ACCOUNT_ID_CHARS,
            ),
            display_name=_text(
                value.get("display_name", value.get("account", "")),
                maximum=MAX_DISPLAY_NAME_CHARS,
            ),
            enabled=bool(value.get("enabled", True)),
            allowed_users=_string_list(value.get("allowed_users")),
            allow_all=bool(value.get("allow_all", False)),
            needs_reauth=bool(value.get("needs_reauth", False)),
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "type": "wechat_ilink",
            "enabled": self.enabled,
            "account_id": self.account_id,
            "user_id": self.user_id,
            "account": self.display_name,
            "bot_token": self.bot_token,
            "base_url": self.base_url,
            "allowed_users": list(self.allowed_users),
            "allow_all": self.allow_all,
            "needs_reauth": self.needs_reauth,
        }

    def public(self, *, default: bool = False) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "display_name": self.display_name or self.user_id or self.account_id,
            "enabled": self.enabled,
            "default": default,
            "allowed_users": list(self.allowed_users),
            "allow_all": self.allow_all,
            "needs_reauth": self.needs_reauth,
        }


def default_settings(secrets: SecretStore) -> dict[str, Any]:
    value = secrets.get(DEFAULT_PROFILE) or {}
    if not isinstance(value, dict):
        return {"enabled": True, "account_id": ""}
    account_id = value.get("account_id", value.get("accountId", ""))
    try:
        account_id = _text(account_id, maximum=MAX_ACCOUNT_ID_CHARS)
    except ProfileError:
        account_id = ""
    return {"enabled": bool(value.get("enabled", True)), "account_id": account_id}


def iter_accounts(secrets: SecretStore) -> Iterator[AccountProfile]:
    for meta in secrets.status():
        profile_name = str(meta.get("profile") or "")
        if not profile_name.startswith(PROFILE_PREFIX):
            continue
        suffix = profile_name[len(PROFILE_PREFIX) :]
        value = secrets.get(profile_name)
        if not isinstance(value, dict):
            continue
        try:
            profile = AccountProfile.parse(value, fallback_id=suffix)
            # A copied profile may contain an account_id that does not match its key.
            # Treat the key as the authority and skip ambiguous entries.
            if account_profile_key(profile.account_id) != profile_name:
                continue
        except ProfileError:
            continue
        yield profile


def get_account(secrets: SecretStore, account_id: str) -> Optional[AccountProfile]:
    key = account_profile_key(account_id)
    value = secrets.get(key)
    if not isinstance(value, dict):
        return None
    profile = AccountProfile.parse(value, fallback_id=account_id)
    if profile.account_id != account_id:
        raise ProfileError("profile account id mismatch")
    return profile


def save_account(secrets: SecretStore, profile: AccountProfile) -> AccountProfile:
    key = account_profile_key(profile.account_id)
    secrets.put(key, profile.canonical())
    current_default = default_settings(secrets)
    if not current_default["account_id"]:
        secrets.put(
            DEFAULT_PROFILE,
            {"type": "wechat_ilink", "enabled": True, "account_id": profile.account_id},
        )
    return profile


def save_confirmation(
    secrets: SecretStore,
    *,
    account_id: str,
    bot_token: str,
    base_url: str,
    user_id: str = "",
    display_name: str = "",
) -> AccountProfile:
    existing = get_account(secrets, account_id)
    profile = AccountProfile(
        account_id=account_id,
        bot_token=_text(bot_token, maximum=16 * 1024, required=True),
        base_url=_text(base_url, maximum=2 * 1024, required=True),
        user_id=_text(user_id, maximum=MAX_ACCOUNT_ID_CHARS),
        display_name=_text(display_name, maximum=MAX_DISPLAY_NAME_CHARS),
        enabled=True,
        allowed_users=existing.allowed_users if existing else (),
        allow_all=existing.allow_all if existing else False,
        needs_reauth=False,
    )
    return save_account(secrets, profile)


def set_needs_reauth(secrets: SecretStore, account_id: str, value: bool = True) -> bool:
    profile = get_account(secrets, account_id)
    if profile is None:
        return False
    save_account(
        secrets,
        AccountProfile(
            account_id=profile.account_id,
            bot_token=profile.bot_token,
            base_url=profile.base_url,
            user_id=profile.user_id,
            display_name=profile.display_name,
            enabled=profile.enabled,
            allowed_users=profile.allowed_users,
            allow_all=profile.allow_all,
            needs_reauth=value,
        ),
    )
    return True


def set_default(secrets: SecretStore, account_id: str) -> bool:
    if get_account(secrets, account_id) is None:
        return False
    enabled = default_settings(secrets)["enabled"]
    secrets.put(
        DEFAULT_PROFILE,
        {"type": "wechat_ilink", "enabled": enabled, "account_id": account_id},
    )
    return True


def delete_account(secrets: SecretStore, account_id: str) -> bool:
    deleted = secrets.delete(account_profile_key(account_id))
    settings = default_settings(secrets)
    if settings["account_id"] == account_id:
        remaining = list(iter_accounts(secrets))
        if remaining:
            secrets.put(
                DEFAULT_PROFILE,
                {
                    "type": "wechat_ilink",
                    "enabled": settings["enabled"],
                    "account_id": remaining[0].account_id,
                },
            )
        else:
            secrets.delete(DEFAULT_PROFILE)
    return deleted


def delete_all(secrets: SecretStore) -> int:
    keys = [str(m.get("profile") or "") for m in secrets.status()]
    deleted = sum(
        bool(secrets.delete(key)) for key in keys if key.startswith(PROFILE_PREFIX)
    )
    secrets.delete(DEFAULT_PROFILE)
    return deleted


def account_rows(secrets: SecretStore) -> list[dict[str, Any]]:
    default_id = default_settings(secrets)["account_id"]
    return [p.public(default=p.account_id == default_id) for p in iter_accounts(secrets)]
