from __future__ import annotations

import asyncio
from collections import deque

from coworker.connectors.base import SendResult
from coworker.connectors.wechat_ilink.adapter import WeChatIlinkAdapter
from coworker.connectors.wechat_ilink.models import IlinkMessage, MessageItem, SendResponse, Updates
from coworker.connectors.wechat_ilink.profiles import save_confirmation
from coworker.secrets import SecretStore


class FakeRuntimeClient:
    def __init__(self, updates=(), *, send_response=None, block_when_empty=True):
        self.updates = deque(updates)
        self.send_response = send_response or SendResponse(ret=0, errcode=None)
        self.block_when_empty = block_when_empty
        self.sent = []
        self.cursors = []
        self.closed = False
        self.release = asyncio.Event()

    async def get_updates(self, _bot_token, cursor=""):
        self.cursors.append(cursor)
        if self.updates:
            value = self.updates.popleft()
            if isinstance(value, Exception):
                raise value
            return value
        if self.block_when_empty:
            await self.release.wait()
        return Updates(ret=0, errcode=None, cursor=cursor, messages=(), longpolling_timeout_ms=None)

    async def send_text(self, **kwargs):
        self.sent.append(kwargs)
        return self.send_response

    async def aclose(self):
        self.closed = True
        self.release.set()


def _updates(*messages, cursor="next", ret=0, errcode=None):
    return Updates(
        ret=ret,
        errcode=errcode,
        cursor=cursor,
        messages=tuple(messages),
        longpolling_timeout_ms=35000,
    )


def _message(mid=1, *, user="friend", context="ctx", items=None):
    return IlinkMessage(
        from_user_id=user,
        to_user_id="bot",
        message_id=mid,
        message_type=1,
        message_state=None,
        context_token=context,
        items=tuple(items or [MessageItem(kind=1, text="hello")]),
    )


def _secrets(tmp_path, *accounts):
    store = SecretStore(tmp_path / "secrets.json")
    for account in accounts or ("account-A",):
        save_confirmation(
            store,
            account_id=account,
            bot_token=f"token-{account}",
            base_url="https://ilinkai.weixin.qq.com",
        )
    return store


async def _eventually(predicate, tries=100):
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition was not reached")


async def test_adapter_maps_account_into_target_and_sends_with_live_context(tmp_path):
    message = _message(items=[MessageItem(kind=3, voice_text="voice transcript")])
    client = FakeRuntimeClient([_updates(message)])
    received = []
    adapter = WeChatIlinkAdapter(
        _secrets(tmp_path), client_factory=lambda _profile: client
    )

    async def capture(event):
        received.append(event)

    adapter.set_message_handler(capture)
    assert await adapter.connect()
    await _eventually(lambda: len(received) == 1)
    event = received[0]
    assert event.text == "voice transcript"
    assert event.source.team_id == "account-A"
    assert event.source.chat_id == "account-A/friend"
    assert event.source.target == "wechat_ilink:account-A/friend"

    result = await adapter.send("account-A/friend", "reply")
    assert result.ok
    assert client.sent[0]["bot_token"] == "token-account-A"
    assert client.sent[0]["context_token"] == "ctx"
    assert client.sent[0]["to_user_id"] == "friend"
    await adapter.disconnect()
    assert client.closed


async def test_adapter_rejects_thread_bad_target_and_missing_context(tmp_path):
    client = FakeRuntimeClient([])
    adapter = WeChatIlinkAdapter(_secrets(tmp_path), client_factory=lambda _p: client)
    assert await adapter.connect()
    assert not (await adapter.send("bad", "x")).ok
    assert not (await adapter.send("account-A/friend", "x", thread_id="thread")).ok
    no_context = await adapter.send("account-A/friend", "x")
    assert not no_context.ok and "no live context" in no_context.error
    await adapter.disconnect()


async def test_adapter_deduplicates_last_200_message_ids(tmp_path):
    duplicated = _message(mid=7)
    client = FakeRuntimeClient([_updates(duplicated, duplicated)])
    received = []
    adapter = WeChatIlinkAdapter(_secrets(tmp_path), client_factory=lambda _p: client)
    adapter.set_message_handler(lambda event: _append(received, event))
    await adapter.connect()
    await _eventually(lambda: len(received) == 1)
    await asyncio.sleep(0.005)
    assert len(received) == 1
    await adapter.disconnect()


async def _append(items, item):
    items.append(item)


async def test_adapter_session_expiry_clears_context_and_marks_profile(tmp_path):
    client = FakeRuntimeClient(
        [_updates(_message()), _updates(ret=-14, errcode=-14, cursor="")],
        block_when_empty=False,
    )
    adapter = WeChatIlinkAdapter(_secrets(tmp_path), client_factory=lambda _p: client)
    adapter.set_message_handler(lambda _event: asyncio.sleep(0))
    await adapter.connect()
    await _eventually(
        lambda: adapter.account_status("account-A")["state"] == "auth_required"
    )
    status = adapter.account_status("account-A")
    assert status["needs_reauth"] is True
    assert not (await adapter.send("account-A/friend", "reply")).ok
    assert adapter._runtimes["account-A"].context_tokens == {}
    assert adapter._runtimes["account-A"].cursor == ""
    profile = adapter._secrets.get("wechat_ilink:account:account-A")
    assert profile["needs_reauth"] is True
    await adapter.disconnect()


async def test_adapter_multiple_accounts_are_isolated(tmp_path):
    clients = {
        "A": FakeRuntimeClient([_updates(_message(user="same", context="ctx-A"))]),
        "B": FakeRuntimeClient([_updates(_message(user="same", context="ctx-B"))]),
    }
    events = []
    adapter = WeChatIlinkAdapter(
        _secrets(tmp_path, "A", "B"),
        client_factory=lambda profile: clients[profile.account_id],
    )
    adapter.set_message_handler(lambda event: _append(events, event))
    await adapter.connect()
    await _eventually(lambda: len(events) == 2)
    assert {e.source.target for e in events} == {
        "wechat_ilink:A/same",
        "wechat_ilink:B/same",
    }
    assert (await adapter.send("A/same", "one")).ok
    assert (await adapter.send("B/same", "two")).ok
    assert clients["A"].sent[0]["context_token"] == "ctx-A"
    assert clients["B"].sent[0]["context_token"] == "ctx-B"
    await adapter.disconnect()


async def test_adapter_disconnect_closes_client_when_worker_has_not_started(tmp_path):
    client = FakeRuntimeClient([])
    adapter = WeChatIlinkAdapter(_secrets(tmp_path), client_factory=lambda _p: client)
    assert await adapter.connect()
    # No event-loop yield occurs between task creation and disconnect, so the poll
    # coroutine may never enter its own finally block.
    await adapter.disconnect()
    assert client.closed


async def test_adapter_returns_real_send_result_and_handles_expiry(tmp_path):
    client = FakeRuntimeClient(
        [_updates(_message())], send_response=SendResponse(ret=-14, errcode=-14)
    )
    adapter = WeChatIlinkAdapter(_secrets(tmp_path), client_factory=lambda _p: client)
    adapter.set_message_handler(lambda _event: asyncio.sleep(0))
    await adapter.connect()
    await _eventually(lambda: bool(adapter._runtimes["account-A"].context_tokens))
    result = await adapter.send("account-A/friend", "reply")
    assert not result.ok and "reauthentication" in result.error
    assert adapter.account_status("account-A")["state"] == "auth_required"
    await adapter.disconnect()
