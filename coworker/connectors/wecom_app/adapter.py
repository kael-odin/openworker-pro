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
from typing import Optional

from ..base import BasePlatformAdapter, MessageEvent, SendResult
from .provider import WeComAppClient, client_from_profile

logger = logging.getLogger("coworker.connectors.wecom")


class WeComAppAdapter(BasePlatformAdapter):
    """企业微信自建应用适配器。

    platform = "wecom"。inbound 走 HTTP 回调（非 socket），所以 connect() 仅验证凭证。
    """

    platform = "wecom"

    def __init__(self, client: WeComAppClient) -> None:
        super().__init__()
        self.client = client
        self._connected = False

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

    # -- inbound（由 HTTP webhook 端点驱动）--------------------------------------
    async def handle_callback(self, query: dict, body: bytes) -> tuple[int, str]:
        """处理企微回调请求，返回 (http_status, response_body)。

        GET（URL 验证）：返回 echostr。
        POST（消息接收）：解密 → 解析 → handle_message → 路由到 agent，回复 "success"。
        """
        ok, payload = self.client.verify_and_decrypt(query, body)
        if not ok:
            logger.warning("wecom 回调校验失败: %s", payload)
            return 403, payload

        # GET 验证：直接回 echostr 明文。
        if not body and query.get("echostr"):
            return 200, payload

        # POST 消息：解密后 payload 是消息 XML。
        try:
            event = self.client.parse_inbound_xml(payload)
        except Exception as exc:
            logger.exception("wecom 消息解析失败")
            return 200, "success"  # 解析失败也回 success，防企微重试轰炸

        # 路由到 gateway（self.handle_message 由基类提供，调 _handler）。
        try:
            await self.handle_message(event)
        except Exception:
            logger.exception("wecom inbound 分发失败")

        # 企微要求 5 秒内响应，且响应 success 即可（不要求加密回复）。
        return 200, "success"

    def status(self) -> dict:
        """健康快照（供 GUI）。"""
        return {
            "connected": self._connected,
            "corpid": self.client.corpid,
            "agent_id": self.client.agent_id,
            "has_token": bool(self.client.token),
            "has_aes_key": bool(self.client.encoding_aes_key),
            "encrypted": bool(self.client.encoding_aes_key),
        }
