"""通知渠道配置存储 —— 密钥走 ``SecretStore``，不落明文。

每个渠道一份配置（webhook URL / SMTP 密码 / 签名 secret），以 ``notify:<channel>`` 为
profile 存入 SecretStore。``list_channels`` 返回脱敏状态（哪些已配置、是否启用），
``get_config`` 返回完整配置（含密钥，仅供后端发送时用），``save_config`` 写入。

渠道配置里有一个 ``enabled`` 字段控制是否参与分发，其余字段是渠道专属的连接参数。
"""

from __future__ import annotations

from typing import Any, Optional

from ..secrets import SecretStore

# 渠道类型 + 显示元数据（label/description 给前端渲染用）。
CHANNEL_TYPES = ["dingtalk", "feishu", "wecom", "webhook", "email"]

CHANNEL_META: dict[str, dict[str, str]] = {
    "dingtalk": {"label": "钉钉", "description": "通过钉钉群机器人 webhook 推送"},
    "feishu": {"label": "飞书", "description": "通过飞书/Lark 群机器人 webhook 推送"},
    "wecom": {"label": "企业微信", "description": "通过企业微信群机器人 webhook 推送"},
    "webhook": {"label": "通用 Webhook", "description": "POST JSON 到任意 URL"},
    "email": {"label": "邮件", "description": "通过 SMTP 发送邮件通知"},
}

# 各渠道配置里属于"敏感"的字段；webhook URL 自带 token/key，也按 secret 处理。
MASK_PLACEHOLDER = "••••••"
_MASK_PLACEHOLDERS = {MASK_PLACEHOLDER, "******", "********"}
_SENSITIVE_FIELDS = {"url", "secret", "password", "token"}
_CHANNEL_FIELDS: dict[str, set[str]] = {
    "dingtalk": {"url", "secret"},
    "feishu": {"url", "secret"},
    "wecom": {"url", "secret"},
    "webhook": {"url", "headers", "template", "allow_http"},
    "email": {
        "smtp_host",
        "smtp_port",
        "username",
        "password",
        "from_addr",
        "to_addr",
        "use_ssl",
    },
}
_MAX_STRING_CHARS = 16_384


def _profile(channel: str) -> str:
    return f"notify:{channel}"


class NotifyConfigStore:
    """通知渠道配置的读写门面，底层是 SecretStore。"""

    def __init__(self, secrets: Optional[SecretStore] = None) -> None:
        self.secrets = secrets or SecretStore()

    def list_channels(self) -> list[dict[str, Any]]:
        """脱敏列表：每个渠道的 enabled 状态 + 是否已配置 + 显示元数据。"""
        out: list[dict[str, Any]] = []
        for ch in CHANNEL_TYPES:
            cfg = self.secrets.get(_profile(ch)) or {}
            out.append(
                {
                    "channel": ch,
                    "label": CHANNEL_META[ch]["label"],
                    "description": CHANNEL_META[ch]["description"],
                    "enabled": bool(cfg.get("enabled")),
                    "configured": bool(cfg),
                }
            )
        return out

    def get_config(self, channel: str) -> dict[str, Any]:
        """完整配置（含密钥）—— 仅供后端发送时读取。"""
        return self.secrets.get(_profile(channel)) or {}

    def save_config(
        self,
        channel: str,
        config: dict[str, Any],
        *,
        clear_fields: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Patch/merge 配置；省略或 mask 值保留旧 secret。"""
        if channel not in _CHANNEL_FIELDS:
            raise ValueError("未知通知渠道")
        if not isinstance(config, dict):
            raise ValueError("config 必须是对象")
        allowed = _CHANNEL_FIELDS[channel]
        unknown = set(config) - allowed - {"enabled", "type"}
        if unknown:
            raise ValueError("配置包含不支持的字段")
        clear = clear_fields or []
        if not isinstance(clear, list) or any(
            not isinstance(field, str) or field not in allowed for field in clear
        ):
            raise ValueError("clear_fields 包含不支持的字段")

        merged = dict(self.get_config(channel))
        merged.setdefault("type", channel)
        for key, value in config.items():
            if key == "type":
                continue
            if key == "enabled":
                if not isinstance(value, bool):
                    raise ValueError("enabled 必须是布尔值")
                merged[key] = value
                continue
            if key in _SENSITIVE_FIELDS and value in _MASK_PLACEHOLDERS:
                continue
            if isinstance(value, str) and len(value) > _MAX_STRING_CHARS:
                raise ValueError("配置字段过长")
            merged[key] = value
        for key in clear:
            merged.pop(key, None)
        self.secrets.put(_profile(channel), merged)
        return merged

    def set_enabled(self, channel: str, enabled: bool) -> dict[str, Any]:
        """只修改 enabled，不要求客户端读回或提交其它配置。"""
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        return self.save_config(channel, {"enabled": enabled})

    def merge_for_test(self, channel: str, patch: dict[str, Any]) -> dict[str, Any]:
        """把未持久化表单 patch 合并到服务端 secret，仅用于一次测试发送。"""
        if channel not in _CHANNEL_FIELDS:
            raise ValueError("未知通知渠道")
        if not isinstance(patch, dict):
            raise ValueError("config 必须是对象")
        allowed = _CHANNEL_FIELDS[channel]
        if set(patch) - allowed - {"enabled", "type"}:
            raise ValueError("配置包含不支持的字段")
        merged = dict(self.get_config(channel))
        for key, value in patch.items():
            if key in ("type", "enabled"):
                continue
            if key in _SENSITIVE_FIELDS and value in _MASK_PLACEHOLDERS:
                continue
            if isinstance(value, str) and len(value) > _MAX_STRING_CHARS:
                raise ValueError("配置字段过长")
            merged[key] = value
        return merged

    def delete_config(self, channel: str) -> bool:
        return self.secrets.delete(_profile(channel))

    @staticmethod
    def mask(config: dict[str, Any]) -> dict[str, Any]:
        """脱敏一份配置：敏感字段用占位符替换，供前端回显。"""
        masked: dict[str, Any] = {}
        for k, v in config.items():
            if k in _SENSITIVE_FIELDS and v:
                masked[k] = MASK_PLACEHOLDER
            else:
                masked[k] = v
        return masked
