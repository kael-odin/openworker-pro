"""通用 webhook 渠道 — POST JSON 到任意 URL。

最灵活的兜底渠道：用户填一个 URL，可选自定义 headers / body 模板。
默认 body 形如 ``{"title": ..., "body": ..., "status": ...}``，多数自建接收端都能直接消费。
"""

from __future__ import annotations

from typing import Any

from .._result import NotifyResult
from ..http import WebhookPostError, post_json


def send(title: str, body: str, config: dict[str, Any], *, status: str = "") -> NotifyResult:
    """POST 一个 JSON 到 ``config["url"]``。

    config:
        url:     必填，目标 webhook URL
        headers: 可选，额外请求头（dict）
        template: 可选，自定义 body 模板（dict）；缺省用 ``{title, body, status}``
    """
    url = (config.get("url") or "").strip()
    if not url:
        return NotifyResult(ok=False, error="webhook url 未配置")

    payload: dict[str, Any] = config.get("template") or {
        "title": title,
        "body": body,
        "status": status,
    }
    try:
        status_code, _ = post_json(
            url,
            payload,
            headers=config.get("headers"),
            allow_http=config.get("allow_http") is True,
        )
    except WebhookPostError as exc:
        return NotifyResult(ok=False, error=str(exc))
    if 200 <= status_code < 300:
        return NotifyResult(ok=True)
    return NotifyResult(ok=False, error=f"webhook HTTP {status_code}")
