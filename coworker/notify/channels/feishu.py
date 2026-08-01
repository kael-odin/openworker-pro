"""飞书/Lark 群机器人 webhook 渠道。

支持加签（timestamp + secret 拼接做 SHA256）。
文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot

config:
    url:    必填，群机器人的 webhook URL
    secret: 可选，加签密钥（机器人安全设置里的签名校验密钥）
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from .._result import NotifyResult
from ..http import VENDOR_HOSTS, WebhookPostError, post_json


def _sign(secret: str) -> tuple[str, str]:
    """飞书加签：返回 (timestamp, sign)。

    算法：key = ``timestamp + "\\n" + secret``，对空消息做 HMAC-SHA256，取十六进制大写。
    """
    timestamp = str(round(time.time()))
    key = f"{timestamp}\n{secret}"
    sign = hmac.new(key.encode("utf-8"), b"", digestmod=hashlib.sha256).hexdigest().upper()
    return timestamp, sign


def send(title: str, body: str, config: dict[str, Any], *, status: str = "") -> NotifyResult:
    url = (config.get("url") or "").strip()
    if not url:
        return NotifyResult(ok=False, channel="feishu", error="飞书 webhook url 未配置")

    text = f"**{title}**\n{body}"
    if status:
        text += f"\n状态: {status}"
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": text},
    }
    secret = config.get("secret") or ""
    if secret:
        timestamp, sign = _sign(secret)
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    try:
        status_code, data = post_json(
            url, payload, allowed_hosts=VENDOR_HOSTS["feishu"]
        )
    except WebhookPostError as exc:
        return NotifyResult(ok=False, channel="feishu", error=str(exc))
    if not 200 <= status_code < 300:
        return NotifyResult(
            ok=False, channel="feishu", error=f"飞书 webhook HTTP {status_code}"
        )
    if not isinstance(data, dict):
        data = {}
    # 飞书成功时 code=0（或 StatusCode=0）；失败时 code 非 0 带 msg。
    code = data.get("code", data.get("StatusCode", -1))
    if code == 0:
        return NotifyResult(ok=True, channel="feishu")
    return NotifyResult(
        ok=False,
        channel="feishu",
        error=f"飞书 code={code}: {data.get('msg') or data.get('StatusMessage')}",
    )
