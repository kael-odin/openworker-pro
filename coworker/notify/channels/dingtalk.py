"""钉钉群机器人 webhook 渠道。

支持加签（timestamp + secret）—— 钉钉机器人的"加签"安全设置。
文档：https://open.dingtalk.com/document/robots/custom-robot-access

config:
    url:    必填，群机器人的 webhook URL（含 access_token）
    secret: 可选，加签密钥（机器人在"安全设置"里选"加签"时显示的 SEC 开头字符串）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any

from .._result import NotifyResult


def _signed_url(url: str, secret: str) -> str:
    """钉钉加签：把 &timestamp=&sign= 追加到 webhook URL。"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={timestamp}&sign={sign}"


def send(title: str, body: str, config: dict[str, Any], *, status: str = "") -> NotifyResult:
    import httpx

    url = (config.get("url") or "").strip()
    if not url:
        return NotifyResult(ok=False, channel="dingtalk", error="钉钉 webhook url 未配置")

    secret = config.get("secret") or ""
    target = _signed_url(url, secret) if secret else url

    # 钉钉 markdown 消息：title 是通知栏标题，text 是正文（支持 markdown）。
    text = f"### {title}\n\n{body}"
    if status:
        text += f"\n\n> 状态: {status}"
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }

    try:
        resp = httpx.post(target, json=payload, timeout=10.0)
        data = resp.json()
    except Exception as exc:
        return NotifyResult(ok=False, channel="dingtalk", error=str(exc))
    # 钉钉成功时 errcode=0；失败 errcode 非 0 带 errmsg。
    if data.get("errcode") == 0:
        return NotifyResult(ok=True, channel="dingtalk")
    return NotifyResult(
        ok=False,
        channel="dingtalk",
        error=f"钉钉 errcode={data.get('errcode')}: {data.get('errmsg')}",
    )
