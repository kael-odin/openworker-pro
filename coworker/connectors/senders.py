"""Stateless outbound senders — one-shot HTTP POSTs, no SDK, no live connection.

These power the `send_message` tool (and the super-agent's replies). Both Telegram and
Slack outbound are simple HTTP calls, so we use a synchronous `httpx` client and avoid the
heavy SDKs (those are only needed for the inbound listeners). Sync fits the ToolRegistry's
`execute` contract (the engine runs it in a thread).

A `Sender` is `(token, chat_id, text, thread_id) -> SendResult`. The registry is swappable so
tests inject fakes — no network.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from .base import SendResult

Sender = Callable[[str, str, str, Optional[str]], SendResult]

_TIMEOUT = 30.0


def _slack_api_base() -> str:
    """Web API base URL. `SLACK_API_URL` (trailing slash) lets tests / the FakeSlack harness
    redirect outbound sends to a local fake. See platform/docs/FAKE-SLACK-SPEC.md."""
    return os.environ.get("SLACK_API_URL", "https://slack.com/api/")


def _send_telegram(
    token: str, chat_id: str, text: str, thread_id: Optional[str] = None
) -> SendResult:
    import httpx

    payload: dict = {"chat_id": chat_id, "text": text}
    # Telegram's General forum topic is thread_id "1", which sendMessage rejects → omit it.
    if thread_id and thread_id != "1":
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            pass
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:  # network / decode
        return SendResult(False, error=str(exc))
    if data.get("ok"):
        return SendResult(
            True, message_id=str(data.get("result", {}).get("message_id"))
        )
    return SendResult(False, error=data.get("description") or "telegram send failed")


def _send_slack(
    token: str, chat_id: str, text: str, thread_id: Optional[str] = None
) -> SendResult:
    import httpx

    from .slack_addr import split

    # A managed-relay chat_id is team-qualified ("T…/C…"); Slack's API wants the
    # bare channel. The per-team token is selected by the caller (send_message).
    _team, chat_id = split(chat_id)
    payload: dict = {"channel": chat_id, "text": text}
    if thread_id:
        payload["thread_ts"] = thread_id
    try:
        resp = httpx.post(
            f"{_slack_api_base()}chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:
        return SendResult(False, error=str(exc))
    if data.get("ok"):
        return SendResult(True, message_id=data.get("ts"))
    err = data.get("error") or "slack send failed"
    if err == "not_in_channel":
        err = "not_in_channel — invite @OpenWorker to the channel in Slack, then retry"
    return SendResult(False, error=err)


def _send_wecom(
    token: str, chat_id: str, text: str, thread_id: Optional[str] = None
) -> SendResult:
    """企微自建应用出站发送。

    `token` 是 _resolve_token 编码的 profile JSON 串（corpid/secret/agent_id/...），
    不是单一 token —— 企微凭证是三件套，sender 无状态只能通过 token 参数传。
    `chat_id` = 用户 userid（企微应用消息 1v1 私聊）。thread_id 忽略（企微无 thread）。
    """
    import json as _json

    from .wecom_app.provider import WeComAppClient

    try:
        profile = _json.loads(token)
    except (ValueError, TypeError):
        return SendResult(False, error="企微凭证解析失败")
    client = WeComAppClient(
        corpid=profile.get("corpid", ""),
        secret=profile.get("secret", ""),
        agent_id=profile.get("agent_id", ""),
        token=profile.get("token", ""),
        encoding_aes_key=profile.get("encoding_aes_key", ""),
    )
    data = client.send_text(chat_id, text)
    if data.get("errcode") == 0:
        return SendResult(True, message_id=str(data.get("msgid", "")))
    return SendResult(
        False,
        error=f"企微发送失败: {data.get('errmsg')} (code={data.get('errcode')})",
    )


def _slack_blocks(text: str, buttons) -> list[dict]:
    """A Block Kit message: a text section + a row of action buttons (action_id `ocw_<i>`,
    value = the encoded item id + resolution)."""
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if buttons:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": b.label[:75]},
                        "value": b.value,
                        "action_id": f"ocw_{i}",
                    }
                    for i, b in enumerate(buttons)
                ],
            }
        )
    return blocks


def _send_slack_interactive(
    token: str, chat_id: str, text: str, buttons, thread_id: Optional[str] = None
) -> SendResult:
    import httpx

    from .slack_addr import split

    _team, chat_id = split(chat_id)
    payload: dict = {
        "channel": chat_id,
        "text": text,
        "blocks": _slack_blocks(text, buttons),
    }
    if thread_id:
        payload["thread_ts"] = thread_id
    try:
        resp = httpx.post(
            f"{_slack_api_base()}chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:
        return SendResult(False, error=str(exc))
    if data.get("ok"):
        return SendResult(True, message_id=data.get("ts"))
    return SendResult(False, error=data.get("error") or "slack send failed")


DEFAULT_SENDERS: dict[str, Sender] = {
    "telegram": _send_telegram,
    "slack": _send_slack,
    "wecom": _send_wecom,
}


# -- file upload (§34 / UX-016) --------------------------------------------------------
# A FileSender is (token, chat_id, thread_id, filename, data, title, comment) -> SendResult.
FileSender = Callable[
    [str, str, Optional[str], str, bytes, Optional[str], Optional[str]], SendResult
]


def _send_slack_file(
    token: str,
    chat_id: str,
    thread_id: Optional[str],
    filename: str,
    data: bytes,
    title: Optional[str] = None,
    comment: Optional[str] = None,
) -> SendResult:
    """files_upload_v2 (the only non-deprecated path): reserve an upload URL, PUT the
    bytes, then complete into the channel/thread. Slack renders its own previews for
    pdf/csv/images — that's the whole point of sending the file instead of a thumbnail.
    """
    import httpx

    from .slack_addr import split

    _team, chat_id = split(chat_id)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.post(
            f"{_slack_api_base()}files.getUploadURLExternal",
            headers=headers,
            data={"filename": filename, "length": str(len(data))},
            timeout=_TIMEOUT,
        )
        got = resp.json()
        if not got.get("ok"):
            return SendResult(
                False, error=got.get("error") or "slack upload-url failed"
            )
        up = httpx.post(
            got["upload_url"],
            files={"file": (filename, data)},
            timeout=max(_TIMEOUT, 120.0),
        )
        if up.status_code != 200:
            return SendResult(False, error=f"slack upload failed ({up.status_code})")
        complete: dict = {
            "files": [{"id": got["file_id"], "title": title or filename}],
            "channel_id": chat_id,
        }
        if thread_id:
            complete["thread_ts"] = thread_id
        if comment:
            complete["initial_comment"] = comment
        resp = httpx.post(
            f"{_slack_api_base()}files.completeUploadExternal",
            headers=headers,
            json=complete,
            timeout=_TIMEOUT,
        )
        data_out = resp.json()
    except Exception as exc:  # network / decode
        return SendResult(False, error=str(exc))
    if data_out.get("ok"):
        return SendResult(True, message_id=got["file_id"])
    return SendResult(False, error=data_out.get("error") or "slack file send failed")


DEFAULT_FILE_SENDERS: dict[str, FileSender] = {
    "slack": _send_slack_file,
}
