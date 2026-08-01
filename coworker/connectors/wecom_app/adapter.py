"""企微自建应用连接器适配器 —— 实现 BasePlatformAdapter 契约。

与 Slack/Telegram 的关键差异：
  - Slack/Telegram 是主动出站连接（Socket Mode / long-poll）拉消息；
  - 企微是被动入站 HTTP 回调 —— 企微服务器 POST 到我们的 webhook URL。
  - 所以 connect() 不开 socket，只验证凭证（能否获取 access_token）；
    真正的 inbound 由 server/app.py 的 webhook 端点驱动，调 handle_callback()。

出站：send() 调 WeComAppClient.send_text() 发应用消息。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Optional

from ..base import BasePlatformAdapter, InteractionEvent, MessageEvent, SendResult
from .provider import WeComAppClient, client_from_profile

logger = logging.getLogger("coworker.connectors.wecom")
_MESSAGE_DEDUP_TTL_SECONDS = 600
_MESSAGE_DEDUP_MAX = 2048


class WeComAppAdapter(BasePlatformAdapter):
    """企业微信自建应用适配器。

    platform = "wecom"。inbound 走 HTTP 回调（非 socket），所以 connect() 仅验证凭证。
    """

    platform = "wecom"

    def __init__(self, client: WeComAppClient) -> None:
        super().__init__()
        self.client = client
        self._connected = False
        self._message_ids: OrderedDict[str, float] = OrderedDict()

    async def connect(self) -> bool:
        """验证凭证可用（能否获取 access_token）。真正的 inbound 监听由 HTTP 端点驱动。"""
        try:
            result = await asyncio.to_thread(self.client.validate_credentials)
            if result.get("ok"):
                self._connected = True
                logger.info("wecom adapter connected (凭证验证通过, agent=%s)", self.client.agent_id)
                return True
            logger.warning("wecom 凭证验证失败: %s", result.get("error"))
            return False
        except Exception:
            logger.exception("wecom connect 失败")
            return False

    async def disconnect(self) -> None:
        self._connected = False

    async def send(self, chat_id: str, text: str, *, thread_id: Optional[str] = None) -> SendResult:
        """发送应用消息。chat_id = 用户 userid。企微不支持 thread 概念，忽略 thread_id。"""
        try:
            data = await asyncio.to_thread(self.client.send_text, chat_id, text)
        except Exception as exc:
            return SendResult(False, error=f"wecom 发送异常: {exc}")
        if data.get("errcode") == 0:
            return SendResult(True, message_id=str(data.get("msgid", "")))
        return SendResult(False, error=f"企微发送失败: {data.get('errmsg')} (code={data.get('errcode')})")

    async def send_interactive(
        self, chat_id: str, text: str, buttons, *, thread_id: Optional[str] = None
    ) -> SendResult:
        """发 button_list 模板卡片 —— 审批/问答的按钮交互载体。点击后企微回调
        template_card_event，handle_callback 转成 InteractionEvent 走与 Slack/Telegram
        同一条解析路径。"""
        title, _, body = text.partition("\n")
        title = title or text
        body = body or ""
        try:
            data = await asyncio.to_thread(
                self.client.send_template_card, chat_id, title, body, buttons
            )
        except Exception as exc:
            return SendResult(False, error=f"wecom 卡片发送异常: {exc}")
        if data.get("errcode") == 0:
            return SendResult(True, message_id=str(data.get("msgid", "")))
        return SendResult(False, error=f"企微卡片发送失败: {data.get('errmsg')} (code={data.get('errcode')})")

    async def update_message(self, chat_id: str, message_id: str, text: str) -> None:
        """企微不支持编辑已发消息的文本；用 update_taskcard 把卡片的按钮替换为结果状态。
        ``message_id`` 这里其实是卡片的 task_id（parse_template_card_event 设的）。"""
        if not message_id:
            return
        try:
            await asyncio.to_thread(
                self.client.update_taskcard, chat_id, message_id, text
            )
        except Exception:
            logger.debug("wecom update_taskcard failed", exc_info=True)

    def _is_duplicate_message(self, message_id: Optional[str]) -> bool:
        if not message_id:
            return False
        now = time.time()
        cutoff = now - _MESSAGE_DEDUP_TTL_SECONDS
        while self._message_ids:
            _, seen_at = next(iter(self._message_ids.items()))
            if seen_at >= cutoff:
                break
            self._message_ids.popitem(last=False)
        if message_id in self._message_ids:
            return True
        self._message_ids[message_id] = now
        while len(self._message_ids) > _MESSAGE_DEDUP_MAX:
            self._message_ids.popitem(last=False)
        return False

    # -- inbound（由 HTTP webhook 端点驱动）--------------------------------------
    async def handle_callback(self, query: dict, body: bytes) -> tuple[int, str]:
        """处理企微回调请求，返回 (http_status, response_body)。

        GET（URL 验证）：返回 echostr。
        POST（消息接收）：解密 → 解析 → handle_message → 路由到 agent，回复 "success"。
        """
        verified = self.client.verify_and_decrypt(query, body)
        if not verified.ok:
            if verified.error_code == "replay" and body:
                # 企微会重试已经成功接收的回调；确认但不重复分发。
                return 200, "success"
            logger.warning("wecom callback rejected (%s)", verified.error_code or "invalid")
            return 403, "invalid callback"
        payload = verified.payload

        # GET 验证：直接回 echostr 明文。
        if not body and query.get("echostr"):
            return 200, payload

        # POST 消息：解密后 payload 是消息 XML。
        # 先判断是不是模板卡片按钮点击（template_card_event）—— 这是审批/问答按钮的回调，
        # 走 InteractionEvent 路径而非普通消息。
        card_event = self.client.parse_template_card_event(payload)
        if card_event is not None:
            try:
                await self.handle_interaction(card_event)
            except Exception:
                logger.exception("wecom template_card_event 分发失败")
            return 200, "success"

        try:
            event = self.client.parse_inbound_xml(payload)
        except Exception:
            logger.exception("wecom 消息解析失败")
            return 200, "success"  # 解析失败也回 success，防企微重试轰炸
        if self._is_duplicate_message(event.message_id):
            return 200, "success"

        # 路由到 gateway（self.handle_message 由基类提供，调 _handler）。
        try:
            await self.handle_message(event)
        except Exception:
            logger.exception("wecom inbound 分发失败")

        # 企微要求 5 秒内响应，且响应 success 即可（不要求加密回复）。
        return 200, "success"

    def status(self) -> dict:
        """健康快照；旧的不完整配置明确标为仅出站、需要迁移。"""
        inbound_state = self.client.inbound_state
        return {
            "connected": self._connected,
            "corpid": self.client.corpid,
            "agent_id": self.client.agent_id,
            "has_token": bool(self.client.token),
            "has_aes_key": bool(self.client.encoding_aes_key),
            "encrypted": inbound_state == "encrypted",
            "inbound_state": inbound_state,
            "needs_migration": inbound_state != "encrypted",
        }
