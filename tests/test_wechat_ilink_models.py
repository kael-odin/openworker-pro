from __future__ import annotations

import pytest

from coworker.connectors.wechat_ilink.models import (
    IlinkMessage,
    IlinkProtocolError,
    MessageItem,
    QrCode,
    QrStatus,
    SendResponse,
    Updates,
)


def test_qrcode_and_status_require_complete_confirmation():
    qr = QrCode.parse({"qrcode": "poll-secret", "qrcode_img_content": "https://qr"})
    assert qr.transaction == "poll-secret"
    assert qr.image_content == "https://qr"

    with pytest.raises(IlinkProtocolError, match="incomplete_qrcode_confirmation"):
        QrStatus.parse({"status": "confirmed", "ilink_bot_id": "bot-1"})

    confirmed = QrStatus.parse(
        {
            "status": "confirmed",
            "bot_token": "secret",
            "ilink_bot_id": "bot-1",
            "baseurl": "https://ilinkai.weixin.qq.com",
            "ilink_user_id": "wx-user",
        }
    )
    assert confirmed.account_id == "bot-1"
    assert confirmed.user_id == "wx-user"


def test_qrcode_status_rejects_unknown_or_oversize_fields():
    with pytest.raises(IlinkProtocolError, match="invalid_qrcode_status"):
        QrStatus.parse({"status": "maybe"})
    with pytest.raises(IlinkProtocolError, match="invalid_qrcode_transaction"):
        QrCode.parse({"qrcode": "x" * (16 * 1024 + 1)})


def test_message_media_mapping_and_voice_transcript():
    message = IlinkMessage.parse(
        {
            "from_user_id": "u1",
            "message_id": 42,
            "message_type": 1,
            "context_token": "context",
            "item_list": [
                {"type": 1, "text_item": {"text": "hello"}},
                {"type": 2, "image_item": {}},
                {"type": 3, "voice_item": {"text": "transcribed"}},
                {"type": 3},
                {"type": 4, "file_item": {"filename": "report.pdf"}},
                {"type": 5, "video_item": {}},
            ],
        }
    )
    assert message.text().splitlines() == [
        "hello",
        "[Image]",
        "transcribed",
        "[Voice]",
        "[File: report.pdf]",
        "[Video]",
    ]


def test_message_parser_rejects_bool_integer_and_unbounded_arrays():
    with pytest.raises(IlinkProtocolError, match="invalid_message_id"):
        IlinkMessage.parse({"message_id": True})
    with pytest.raises(IlinkProtocolError, match="invalid_message_items"):
        IlinkMessage.parse({"item_list": [{"type": 1}] * 51})
    with pytest.raises(IlinkProtocolError, match="invalid_message_item_type"):
        MessageItem.parse({"type": 99})


def test_updates_codes_cursor_and_session_expiry():
    updates = Updates.parse(
        {
            "errcode": -14,
            "get_updates_buf": "cursor",
            "msgs": [],
            "longpolling_timeout_ms": 35000,
        }
    )
    assert updates.ret == -14
    assert updates.session_expired
    assert updates.cursor == "cursor"

    with pytest.raises(IlinkProtocolError, match="invalid_longpolling_timeout"):
        Updates.parse({"longpolling_timeout_ms": 999999})


def test_send_response_success_and_expiry():
    assert SendResponse.parse({}).ok
    assert not SendResponse.parse({"ret": 1}).ok
    assert SendResponse.parse({"errcode": -14}).session_expired
