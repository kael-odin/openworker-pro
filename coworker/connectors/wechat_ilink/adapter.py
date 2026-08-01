"""Multi-account live adapter for personal WeChat over iLink."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..base import BasePlatformAdapter, MessageEvent, SendResult, SessionSource
from .client import IlinkClient
from .models import IlinkMessage, IlinkProtocolError
from .profiles import AccountProfile, iter_accounts, set_needs_reauth
from .transport import IlinkTransport, IlinkTransportError

logger = logging.getLogger("coworker.connectors.wechat_ilink")

RECONNECT_BASE_SECONDS = 2.0
RECONNECT_CAP_SECONDS = 30.0
MAX_RECONNECT_ATTEMPTS = 100
DEDUP_MAX_SIZE = 200


@dataclass
class AccountRuntime:
    profile: AccountProfile
    client: IlinkClient
    generation: int = 0
    cursor: str = ""
    retry_count: int = 0
    state: str = "offline"
    last_event_at: Optional[float] = None
    last_error: str = ""
    task: Optional[asyncio.Task[None]] = field(default=None, repr=False)
    context_tokens: dict[str, str] = field(default_factory=dict, repr=False)
    seen_order: deque[str] = field(
        default_factory=lambda: deque(maxlen=DEDUP_MAX_SIZE), repr=False
    )
    seen_ids: set[str] = field(default_factory=set, repr=False)

    def seen(self, message_id: str) -> bool:
        if message_id in self.seen_ids:
            return True
        if len(self.seen_order) == self.seen_order.maxlen:
            evicted = self.seen_order.popleft()
            self.seen_ids.discard(evicted)
        self.seen_order.append(message_id)
        self.seen_ids.add(message_id)
        return False

    def clear_live_state(self) -> None:
        self.cursor = ""
        self.context_tokens.clear()
        self.seen_order.clear()
        self.seen_ids.clear()

    def public(self) -> dict:
        return {
            "account_id": self.profile.account_id,
            "display_name": (
                self.profile.display_name
                or self.profile.user_id
                or self.profile.account_id
            ),
            "state": self.state,
            "retry_count": self.retry_count,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "needs_reauth": self.state == "auth_required" or self.profile.needs_reauth,
        }


class WeChatIlinkAdapter(BasePlatformAdapter):
    platform = "wechat_ilink"

    def __init__(
        self,
        secrets,
        *,
        client_factory: Optional[Callable[[AccountProfile], IlinkClient]] = None,
        sleep: Callable[[float], object] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        base_delay: float = RECONNECT_BASE_SECONDS,
        max_delay: float = RECONNECT_CAP_SECONDS,
        max_retries: int = MAX_RECONNECT_ATTEMPTS,
    ) -> None:
        super().__init__()
        self._secrets = secrets
        self._client_factory = client_factory or (
            lambda profile: IlinkClient(IlinkTransport(profile.base_url))
        )
        self._sleep = sleep
        self._jitter = jitter
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._max_retries = max_retries
        self._runtimes: dict[str, AccountRuntime] = {}
        self._generation = 0
        self._connected = False

    async def connect(self) -> bool:
        await self.disconnect()
        self._connected = True
        self._generation += 1
        generation = self._generation
        for profile in iter_accounts(self._secrets):
            if (
                not profile.enabled
                or profile.needs_reauth
                or not profile.bot_token
                or not profile.base_url
            ):
                continue
            runtime = AccountRuntime(profile=profile, client=self._client_factory(profile))
            runtime.generation = generation
            runtime.state = "connecting"
            self._runtimes[profile.account_id] = runtime
            runtime.task = asyncio.create_task(self._poll(runtime, generation))
        return bool(self._runtimes)

    async def disconnect(self) -> None:
        self._connected = False
        self._generation += 1
        runtimes, self._runtimes = list(self._runtimes.values()), {}
        for runtime in runtimes:
            runtime.generation += 1
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
        for runtime in runtimes:
            if runtime.task:
                with contextlib.suppress(asyncio.CancelledError):
                    await runtime.task
            # A newly-created task can be cancelled before `_poll()` executes its
            # `finally`. IlinkClient.aclose is idempotent, so close here as well to
            # guarantee that every runtime releases its AsyncClient.
            await runtime.client.aclose()
            runtime.clear_live_state()
            runtime.state = "offline"

    async def send(
        self, chat_id: str, text: str, *, thread_id: Optional[str] = None
    ) -> SendResult:
        if thread_id:
            return SendResult(False, error="wechat_ilink does not support threads")
        parsed = self._split_chat_id(chat_id)
        if parsed is None:
            return SendResult(
                False, error="invalid wechat_ilink target (expected account/user)"
            )
        account_id, user_id = parsed
        runtime = self._runtimes.get(account_id)
        if runtime is None or not self._connected:
            return SendResult(False, error="wechat_ilink account is not live")
        if runtime.state == "auth_required":
            return SendResult(False, error="wechat_ilink account requires reauthentication")
        context_token = runtime.context_tokens.get(user_id)
        if not context_token:
            return SendResult(
                False,
                error=(
                    "wechat_ilink has no live context for this user; wait for a new inbound message"
                ),
            )
        try:
            response = await runtime.client.send_text(
                bot_token=runtime.profile.bot_token,
                to_user_id=user_id,
                text=text,
                context_token=context_token,
            )
        except (IlinkTransportError, IlinkProtocolError, ValueError):
            runtime.last_error = "send_failed"
            return SendResult(False, error="wechat_ilink send failed")
        if response.session_expired:
            self._mark_auth_required(runtime)
            return SendResult(False, error="wechat_ilink account requires reauthentication")
        if not response.ok:
            runtime.last_error = "send_rejected"
            return SendResult(False, error="wechat_ilink send rejected")
        runtime.last_error = ""
        return SendResult(True)

    def status(self) -> dict:
        return {
            "state": "live" if any(r.state == "live" for r in self._runtimes.values()) else "offline",
            "accounts": {account_id: runtime.public() for account_id, runtime in self._runtimes.items()},
        }

    def account_status(self, account_id: str) -> Optional[dict]:
        runtime = self._runtimes.get(account_id)
        return runtime.public() if runtime else None

    async def _poll(self, runtime: AccountRuntime, generation: int) -> None:
        try:
            while self._current(runtime, generation):
                try:
                    updates = await runtime.client.get_updates(
                        runtime.profile.bot_token, runtime.cursor
                    )
                except asyncio.CancelledError:
                    raise
                except (IlinkTransportError, IlinkProtocolError, ValueError):
                    if not await self._backoff(runtime, generation, "poll_failed"):
                        return
                    continue

                if not self._current(runtime, generation):
                    return
                if updates.session_expired:
                    self._mark_auth_required(runtime)
                    return
                if updates.ret != 0:
                    if not await self._backoff(runtime, generation, "poll_rejected"):
                        return
                    continue

                runtime.state = "live"
                runtime.retry_count = 0
                runtime.last_error = ""
                if updates.cursor:
                    runtime.cursor = updates.cursor
                for message in updates.messages:
                    if not self._current(runtime, generation):
                        return
                    await self._handle_inbound(runtime, message)
        except asyncio.CancelledError:
            return
        finally:
            await runtime.client.aclose()
            if runtime.state not in ("auth_required", "failed"):
                runtime.state = "offline"

    async def _backoff(
        self, runtime: AccountRuntime, generation: int, error: str
    ) -> bool:
        runtime.retry_count += 1
        runtime.last_error = error
        if runtime.retry_count > self._max_retries:
            runtime.state = "failed"
            runtime.last_error = "retry_exhausted"
            return False
        runtime.state = "reconnecting"
        raw_delay = min(
            self._base_delay * (2 ** (runtime.retry_count - 1)), self._max_delay
        )
        delay = max(0.0, self._jitter(raw_delay * 0.8, raw_delay * 1.2))
        try:
            await self._sleep(delay)  # type: ignore[misc]
        except asyncio.CancelledError:
            return False
        return self._current(runtime, generation)

    async def _handle_inbound(
        self, runtime: AccountRuntime, message: IlinkMessage
    ) -> None:
        if message.message_type != 1 or not message.from_user_id:
            return
        message_id = (
            str(message.message_id)
            if message.message_id is not None
            else self._fallback_message_id(message)
        )
        if runtime.seen(message_id):
            return
        user_id = message.from_user_id
        if message.context_token:
            runtime.context_tokens[user_id] = message.context_token
        text = message.text()
        if not text:
            return
        runtime.last_event_at = time.time()
        await self.handle_message(
            MessageEvent(
                text=text,
                source=SessionSource(
                    platform=self.platform,
                    team_id=runtime.profile.account_id,
                    chat_id=f"{runtime.profile.account_id}/{user_id}",
                    user_id=user_id,
                    user_name=user_id,
                    chat_type="dm",
                ),
                message_id=message_id,
                raw=None,
            )
        )

    def _mark_auth_required(self, runtime: AccountRuntime) -> None:
        runtime.state = "auth_required"
        runtime.last_error = "authentication_expired"
        runtime.clear_live_state()
        with contextlib.suppress(Exception):
            set_needs_reauth(self._secrets, runtime.profile.account_id, True)

    def _current(self, runtime: AccountRuntime, generation: int) -> bool:
        return (
            self._connected
            and generation == self._generation
            and generation == runtime.generation
            and self._runtimes.get(runtime.profile.account_id) is runtime
        )

    @staticmethod
    def _split_chat_id(chat_id: str) -> Optional[tuple[str, str]]:
        if not isinstance(chat_id, str) or chat_id.count("/") != 1:
            return None
        account_id, user_id = chat_id.split("/", 1)
        if not account_id or not user_id:
            return None
        return account_id, user_id

    @staticmethod
    def _fallback_message_id(message: IlinkMessage) -> str:
        # No time/random fallback: a replay of an id-less message must deduplicate.
        # The context is never logged/persisted or exposed; this composite stays in memory.
        return f"{message.from_user_id}:{message.context_token}:{message.text()}"
