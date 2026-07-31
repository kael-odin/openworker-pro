"""通知渠道层 — 任务运行结果推送到钉钉/飞书/企微/通用 webhook/邮件。

设计对齐 `connectors/senders.py`：无状态 httpx POST，同步（引擎/调度器在 thread 里跑），
`Sender` 可替换以便测试注入 fake。每个渠道是 `(title, body, config) -> SendResult`。

router 按数字人的 `output.notify` 配置分发到多渠道，支持 `notification_level`
（all / important / none），与 Halo 的 `userOverrides.notificationLevel` 对齐。
"""

from __future__ import annotations

from .router import NotifyRouter
from .config import NotifyConfigStore, CHANNEL_TYPES

__all__ = ["NotifyRouter", "NotifyConfigStore", "CHANNEL_TYPES"]
