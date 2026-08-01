"""High-level iLink protocol operations."""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from .models import QrCode, QrStatus, SendResponse, Updates
from .transport import CHANNEL_VERSION, IlinkTransport

MAX_OUTBOUND_TEXT_CHARS = 64 * 1024
MAX_USER_ID_CHARS = 512
MAX_CONTEXT_TOKEN_CHARS = 16 * 1024


class IlinkClient:
    def __init__(self, transport: IlinkTransport) -> None:
        self.transport = transport

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def get_bot_qrcode(self) -> QrCode:
        value = await self.transport.get(
            "/ilink/bot/get_bot_qrcode", params={"bot_type": "3"}
        )
        return QrCode.parse(value)

    async def get_qrcode_status(self, transaction: str) -> QrStatus:
        if not isinstance(transaction, str) or not transaction or len(transaction) > 16 * 1024:
            raise ValueError("invalid qrcode transaction")
        value = await self.transport.get(
            "/ilink/bot/get_qrcode_status",
            params={"qrcode": transaction},
            headers={"iLink-App-ClientVersion": "1"},
        )
        return QrStatus.parse(value)

    async def get_updates(self, bot_token: str, cursor: str = "") -> Updates:
        if not isinstance(cursor, str) or len(cursor) > 16 * 1024:
            raise ValueError("invalid updates cursor")
        value = await self.transport.post(
            "/ilink/bot/getupdates",
            bot_token=bot_token,
            json_body={
                "get_updates_buf": cursor,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
        )
        return Updates.parse(value)

    async def send_text(
        self,
        *,
        bot_token: str,
        to_user_id: str,
        text: str,
        context_token: str,
        client_id: Optional[str] = None,
    ) -> SendResponse:
        if (
            not isinstance(to_user_id, str)
            or not to_user_id
            or len(to_user_id) > MAX_USER_ID_CHARS
        ):
            raise ValueError("invalid recipient")
        if not isinstance(text, str) or not text or len(text) > MAX_OUTBOUND_TEXT_CHARS:
            raise ValueError("invalid message text")
        if (
            not isinstance(context_token, str)
            or not context_token
            or len(context_token) > MAX_CONTEXT_TOKEN_CHARS
        ):
            raise ValueError("invalid message context")
        message_id = client_id or str(uuid4())
        value = await self.transport.post(
            "/ilink/bot/sendmessage",
            bot_token=bot_token,
            json_body={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": message_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
        )
        return SendResponse.parse(value)
