"""企业微信群机器人 webhook 渠道。

文档：https://developer.work.weixin.qq.com/document/path/91770

config:
    url:    必填，群机器人的 webhook URL（含 key）
    secret: 可选，加签密钥（机器人安全设置"签名校验"开启时显示的字符串）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from .._result import NotifyResult


def _sign(secret: str) -> tuple[str, str]:
    """企微加签：返回 (timestamp, sign)。

    算法：``sign = Base64(HmacSHA256(key=secret, msg=timestamp + "\\n" + secret))``
    """
    timestamp = str(round(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = base64.b64encode(digest).decode("utf-8")
    return timestamp, sign


def send(title: str, body: str, config: dict[str, Any], *, status: str = "") -> NotifyResult:
    import httpx

    url = (config.get("url") or "").strip()
    if not url:
        return NotifyResult(ok=False, channel="wecom", error="企微 webhook url 未配置")

    text = f"**{title}**\n{body}"
    if status:
        text += f"\n> 状态: {status}"
    payload: dict[str, Any] = {
        "msgtype": "markdown",
        "markdown": {"content": text},
    }
    secret = config.get("secret") or ""
    if secret:
        timestamp, sign = _sign(secret)
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
        data = resp.json()
    except Exception as exc:
        return NotifyResult(ok=False, channel="wecom", error=str(exc))
    # 企微成功时 errcode=0；失败 errcode 非 0 带 errmsg。
    if data.get("errcode") == 0:
        return NotifyResult(ok=True, channel="wecom")
    return NotifyResult(
        ok=False,
        channel="wecom",
        error=f"企微 errcode={data.get('errcode')}: {data.get('errmsg')}",
    )
