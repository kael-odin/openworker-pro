"""Strict protocol models for the personal WeChat iLink connector.

The service does not publish an SDK or stable schema.  These parsers therefore keep
its untrusted JSON at a narrow boundary before the rest of OpenWorker sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

MAX_STRING_CHARS = 64 * 1024
MAX_IDENTIFIER_CHARS = 512
MAX_TOKEN_CHARS = 16 * 1024
MAX_QR_IMAGE_CHARS = 256 * 1024
MAX_MESSAGES = 500
MAX_ITEMS_PER_MESSAGE = 50
MAX_ERROR_CHARS = 2 * 1024

SESSION_EXPIRED_CODE = -14
QR_STATUSES = frozenset({"wait", "scaned", "confirmed", "expired"})


class IlinkProtocolError(ValueError):
    """A bounded, non-secret protocol validation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _object(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IlinkProtocolError(code)
    return value


def _string(
    value: Any,
    code: str,
    *,
    required: bool = False,
    maximum: int = MAX_STRING_CHARS,
) -> str:
    if value is None:
        if required:
            raise IlinkProtocolError(code)
        return ""
    if not isinstance(value, str):
        raise IlinkProtocolError(code)
    if required and not value:
        raise IlinkProtocolError(code)
    if len(value) > maximum:
        raise IlinkProtocolError(code)
    return value


def _integer(value: Any, code: str, *, required: bool = False) -> Optional[int]:
    if value is None:
        if required:
            raise IlinkProtocolError(code)
        return None
    # bool is an int subclass but is never a valid protocol status code/message id.
    if isinstance(value, bool) or not isinstance(value, int):
        raise IlinkProtocolError(code)
    return value


def _array(value: Any, code: str, *, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise IlinkProtocolError(code)
    return value


@dataclass(frozen=True)
class QrCode:
    transaction: str
    image_content: str

    @classmethod
    def parse(cls, value: Any) -> "QrCode":
        obj = _object(value, "invalid_qrcode_response")
        return cls(
            transaction=_string(
                obj.get("qrcode"),
                "invalid_qrcode_transaction",
                required=True,
                maximum=MAX_TOKEN_CHARS,
            ),
            image_content=_string(
                obj.get("qrcode_img_content"),
                "invalid_qrcode_image",
                maximum=MAX_QR_IMAGE_CHARS,
            ),
        )


@dataclass(frozen=True)
class QrStatus:
    status: str
    bot_token: str = ""
    account_id: str = ""
    base_url: str = ""
    user_id: str = ""

    @classmethod
    def parse(cls, value: Any) -> "QrStatus":
        obj = _object(value, "invalid_qrcode_status_response")
        status = _string(
            obj.get("status"), "invalid_qrcode_status", required=True, maximum=32
        )
        if status not in QR_STATUSES:
            raise IlinkProtocolError("invalid_qrcode_status")
        result = cls(
            status=status,
            bot_token=_string(
                obj.get("bot_token"), "invalid_bot_token", maximum=MAX_TOKEN_CHARS
            ),
            account_id=_string(
                obj.get("ilink_bot_id"),
                "invalid_account_id",
                maximum=MAX_IDENTIFIER_CHARS,
            ),
            base_url=_string(
                obj.get("baseurl"), "invalid_base_url", maximum=2 * 1024
            ),
            user_id=_string(
                obj.get("ilink_user_id"),
                "invalid_user_id",
                maximum=MAX_IDENTIFIER_CHARS,
            ),
        )
        if result.status == "confirmed" and (
            not result.bot_token or not result.account_id
        ):
            raise IlinkProtocolError("incomplete_qrcode_confirmation")
        return result


@dataclass(frozen=True)
class MessageItem:
    kind: int
    text: str = ""
    voice_text: str = ""
    filename: str = ""

    @classmethod
    def parse(cls, value: Any) -> "MessageItem":
        obj = _object(value, "invalid_message_item")
        kind = _integer(obj.get("type"), "invalid_message_item_type", required=True)
        if kind not in (1, 2, 3, 4, 5):
            raise IlinkProtocolError("invalid_message_item_type")

        text = ""
        text_item = obj.get("text_item")
        if text_item is not None:
            text = _string(
                _object(text_item, "invalid_text_item").get("text"),
                "invalid_text",
            )

        voice_text = ""
        voice_item = obj.get("voice_item")
        if voice_item is not None:
            voice_text = _string(
                _object(voice_item, "invalid_voice_item").get("text"),
                "invalid_voice_text",
            )

        filename = ""
        file_item = obj.get("file_item")
        if file_item is not None:
            filename = _string(
                _object(file_item, "invalid_file_item").get("filename"),
                "invalid_filename",
                maximum=2 * 1024,
            )
        return cls(kind=kind, text=text, voice_text=voice_text, filename=filename)

    def render(self) -> str:
        if self.kind == 1:
            return self.text
        if self.kind == 2:
            return "[Image]"
        if self.kind == 3:
            return self.voice_text or "[Voice]"
        if self.kind == 4:
            return f"[File: {self.filename or 'unknown'}]"
        return "[Video]"


@dataclass(frozen=True)
class IlinkMessage:
    from_user_id: str
    to_user_id: str
    message_id: Optional[int]
    message_type: Optional[int]
    message_state: Optional[int]
    context_token: str
    items: tuple[MessageItem, ...]

    @classmethod
    def parse(cls, value: Any) -> "IlinkMessage":
        obj = _object(value, "invalid_message")
        items = tuple(
            MessageItem.parse(item)
            for item in _array(
                obj.get("item_list"),
                "invalid_message_items",
                maximum=MAX_ITEMS_PER_MESSAGE,
            )
        )
        return cls(
            from_user_id=_string(
                obj.get("from_user_id"),
                "invalid_from_user_id",
                maximum=MAX_IDENTIFIER_CHARS,
            ),
            to_user_id=_string(
                obj.get("to_user_id"),
                "invalid_to_user_id",
                maximum=MAX_IDENTIFIER_CHARS,
            ),
            message_id=_integer(obj.get("message_id"), "invalid_message_id"),
            message_type=_integer(obj.get("message_type"), "invalid_message_type"),
            message_state=_integer(obj.get("message_state"), "invalid_message_state"),
            context_token=_string(
                obj.get("context_token"),
                "invalid_context_token",
                maximum=MAX_TOKEN_CHARS,
            ),
            items=items,
        )

    def text(self) -> str:
        return "\n".join(part for item in self.items if (part := item.render())).strip()


@dataclass(frozen=True)
class Updates:
    ret: int
    errcode: Optional[int]
    cursor: str
    messages: tuple[IlinkMessage, ...]
    longpolling_timeout_ms: Optional[int]

    @classmethod
    def parse(cls, value: Any) -> "Updates":
        obj = _object(value, "invalid_updates_response")
        ret_value = _integer(obj.get("ret"), "invalid_ret")
        errcode = _integer(obj.get("errcode"), "invalid_errcode")
        ret = ret_value if ret_value is not None else (errcode or 0)
        timeout = _integer(
            obj.get("longpolling_timeout_ms"), "invalid_longpolling_timeout"
        )
        if timeout is not None and (timeout < 0 or timeout > 5 * 60 * 1000):
            raise IlinkProtocolError("invalid_longpolling_timeout")
        messages = tuple(
            IlinkMessage.parse(message)
            for message in _array(
                obj.get("msgs"), "invalid_messages", maximum=MAX_MESSAGES
            )
        )
        return cls(
            ret=ret,
            errcode=errcode,
            cursor=_string(
                obj.get("get_updates_buf"),
                "invalid_updates_cursor",
                maximum=MAX_TOKEN_CHARS,
            ),
            messages=messages,
            longpolling_timeout_ms=timeout,
        )

    @property
    def session_expired(self) -> bool:
        return self.ret == SESSION_EXPIRED_CODE or self.errcode == SESSION_EXPIRED_CODE


@dataclass(frozen=True)
class SendResponse:
    ret: int
    errcode: Optional[int]

    @classmethod
    def parse(cls, value: Any) -> "SendResponse":
        obj = _object(value, "invalid_send_response")
        ret_value = _integer(obj.get("ret"), "invalid_ret")
        errcode = _integer(obj.get("errcode"), "invalid_errcode")
        return cls(
            ret=ret_value if ret_value is not None else (errcode or 0),
            errcode=errcode,
        )

    @property
    def ok(self) -> bool:
        return self.ret == 0 and (self.errcode is None or self.errcode == 0)

    @property
    def session_expired(self) -> bool:
        return self.ret == SESSION_EXPIRED_CODE or self.errcode == SESSION_EXPIRED_CODE
