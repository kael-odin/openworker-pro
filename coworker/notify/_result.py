"""通知渠道的值类型。

与 ``connectors/base.SendResult`` 同构但独立 —— 通知语义是"投递成功与否"，
不需要 ``message_id``，加一个 ``channel`` 字段方便 router 汇总多渠道结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class NotifyResult:
    ok: bool
    channel: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "channel": self.channel, "error": self.error}
