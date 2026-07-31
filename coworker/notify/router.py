"""通知路由 —— 按 notify 配置把一次运行结果分发到多个渠道。

核心决策：
  1. 推哪些渠道：``channels`` 配置里的每个已启用渠道。
  2. 是否推：``notification_level`` + run status。
     - ``none``     → 永不推
     - ``important``→ error 必推；ok 不推（默认）
     - ``all``      → 每次都推
  3. 推什么：title + body（来自 run 的 status / result_text）+ status 标签。

router 不抛异常 —— 单个渠道失败记录到结果，不影响其他渠道，也不影响 run 本身
（调度器侧用 try/except 兜底，通知是 best-effort）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ._result import NotifyResult
from .channels import CHANNELS
from .config import NotifyConfigStore

logger = logging.getLogger("coworker.notify")

# 默认级别：只推重要事件（error）。与 Halo userOverrides.notificationLevel 默认 'important' 对齐。
DEFAULT_LEVEL = "important"


@dataclass
class NotifyDispatch:
    """一次分发的汇总结果。"""

    results: list[NotifyResult] = field(default_factory=list)

    @property
    def any_ok(self) -> bool:
        return any(r.ok for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.any_ok,
            "results": [r.to_dict() for r in self.results],
        }


class NotifyRouter:
    """读取渠道配置 + 运行结果，分发到各渠道。"""

    def __init__(self, store: Optional[NotifyConfigStore] = None) -> None:
        self.store = store or NotifyConfigStore()

    def _should_send(self, level: str, status: str) -> bool:
        if level == "none":
            return False
        if level == "all":
            return True
        # important: error 必推，其余不推。
        return status == "error"

    def dispatch(
        self,
        *,
        title: str,
        body: str,
        status: str = "ok",
        channels: Optional[list[str]] = None,
        level: str = DEFAULT_LEVEL,
    ) -> NotifyDispatch:
        """分发到指定渠道（None = 所有已启用渠道）。"""
        if not self._should_send(level, status):
            return NotifyDispatch()

        # 没指定渠道 → 取所有已启用渠道。
        if channels is None:
            channels = [
                c["channel"] for c in self.store.list_channels() if c["enabled"]
            ]

        results: list[NotifyResult] = []
        for ch in channels:
            sender = CHANNELS.get(ch)
            if sender is None:
                results.append(
                    NotifyResult(ok=False, channel=ch, error=f"未知渠道 {ch!r}")
                )
                continue
            cfg = self.store.get_config(ch)
            if not cfg:
                results.append(
                    NotifyResult(ok=False, channel=ch, error=f"渠道 {ch} 未配置")
                )
                continue
            if not cfg.get("enabled"):
                continue  # 显式指定但未启用 → 跳过
            try:
                r = sender(title, body, cfg, status=status)
                r.channel = ch
                results.append(r)
            except Exception as exc:  # 单渠道失败不阻断其他
                logger.exception("notify channel %s failed", ch)
                results.append(NotifyResult(ok=False, channel=ch, error=str(exc)))
        return NotifyDispatch(results=results)

    def dispatch_run(
        self,
        *,
        task_name: str,
        run_status: str,
        result_text: Optional[str],
        error: Optional[str] = None,
        channels: Optional[list[str]] = None,
        level: str = DEFAULT_LEVEL,
    ) -> NotifyDispatch:
        """便捷封装：从一个 automation run 构造 title/body 并分发。"""
        title = f"[{task_name}] {'失败' if run_status == 'error' else '完成'}"
        body = error or result_text or "(无输出)"
        return self.dispatch(
            title=title,
            body=body[:2000],  # 截断，避免超长通知
            status=run_status,
            channels=channels,
            level=level,
        )

    def test_send(self, channel: str, config: dict[str, Any]) -> NotifyDispatch:
        """用给定配置（非持久化的）测试发送一条 —— 供配置面板"测试"按钮。"""
        sender = CHANNELS.get(channel)
        if sender is None:
            return NotifyDispatch(
                results=[NotifyResult(ok=False, channel=channel, error=f"未知渠道 {channel!r}")]
            )
        try:
            r = sender("OpenWorker 通知测试", "这是一条测试通知，配置成功。", config, status="ok")
            r.channel = channel
            return NotifyDispatch(results=[r])
        except Exception as exc:
            return NotifyDispatch(
                results=[NotifyResult(ok=False, channel=channel, error=str(exc))]
            )
