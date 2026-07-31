"""通知渠道实现 —— 每个模块导出 ``send(title, body, config, *, status)``。

注册表 ``CHANNELS`` 把渠道名映射到 send 函数。router 据此分发。
"""

from __future__ import annotations

from typing import Any, Callable

from .._result import NotifyResult

Sender = Callable[..., NotifyResult]

from . import dingtalk, feishu, wecom_webhook, generic_webhook, email

CHANNELS: dict[str, Sender] = {
    "dingtalk": dingtalk.send,
    "feishu": feishu.send,
    "wecom": wecom_webhook.send,
    "webhook": generic_webhook.send,
    "email": email.send,
}

__all__ = ["CHANNELS", "Sender", "NotifyResult"]
