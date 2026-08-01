"""Backend-owned QR login attempts for WeChat iLink.

Only the scannable QR content crosses the API boundary.  The polling transaction and
confirmed bot credential remain in this registry/SecretStore process boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import secrets as random_secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ...secrets import SecretStore
from .client import IlinkClient
from .models import IlinkProtocolError
from .profiles import ProfileError, save_confirmation
from .transport import DEFAULT_BASE_URL, IlinkTransport, IlinkTransportError, validate_base_url

PUBLIC_STATUSES = frozenset(
    {"waiting", "scanned", "confirmed", "expired", "failed", "cancelled"}
)
TERMINAL_STATUSES = frozenset({"confirmed", "expired", "failed", "cancelled"})


@dataclass
class QrAttempt:
    attempt_id: str
    expires_at: float
    status: str = "waiting"
    qr_content: str = ""
    error: str = ""
    account_id: str = ""
    display_name: str = ""
    reauth_account_id: str = ""
    generation: int = 0
    _transaction: str = field(default="", repr=False)
    _client: Optional[IlinkClient] = field(default=None, repr=False)
    _task: Optional[asyncio.Task[None]] = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "status": self.status,
            "expires_at": self.expires_at,
        }
        if self.status in ("waiting", "scanned") and self.qr_content:
            out["qr_content"] = self.qr_content
        if self.status == "confirmed":
            out["account_id"] = self.account_id
            out["display_name"] = self.display_name or self.account_id
        if self.error:
            out["error"] = self.error
        return out


class QrAttemptRegistry:
    def __init__(
        self,
        secrets: SecretStore,
        *,
        client_factory: Optional[Callable[[], IlinkClient]] = None,
        on_confirm: Optional[Callable[[str], Any]] = None,
        ttl_seconds: float = 300.0,
        poll_interval: float = 2.0,
        terminal_retention: float = 300.0,
    ) -> None:
        self._secrets = secrets
        self._client_factory = client_factory or (
            lambda: IlinkClient(IlinkTransport(DEFAULT_BASE_URL))
        )
        self._on_confirm = on_confirm
        self._ttl_seconds = ttl_seconds
        self._poll_interval = poll_interval
        self._terminal_retention = terminal_retention
        self._attempts: dict[str, QrAttempt] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, reauth_account_id: str = "") -> dict[str, Any]:
        await self._prune()
        client = self._client_factory()
        try:
            qrcode = await client.get_bot_qrcode()
        except Exception:
            await client.aclose()
            raise IlinkTransportError("qrcode_unavailable") from None

        if not qrcode.image_content:
            # The transaction is the status-polling credential, not display data. Never
            # expose it as a renderer fallback when the service omits QR image content.
            await client.aclose()
            raise IlinkTransportError("qrcode_image_unavailable")

        now = time.time()
        attempt = QrAttempt(
            attempt_id=random_secrets.token_urlsafe(24),
            expires_at=now + self._ttl_seconds,
            qr_content=qrcode.image_content,
            reauth_account_id=reauth_account_id.strip(),
            _transaction=qrcode.transaction,
            _client=client,
        )
        async with self._lock:
            self._attempts[attempt.attempt_id] = attempt
            attempt._task = asyncio.create_task(self._poll(attempt))
        return attempt.public()

    async def get(self, attempt_id: str) -> Optional[dict[str, Any]]:
        await self._prune()
        async with self._lock:
            attempt = self._attempts.get(attempt_id)
            return attempt.public() if attempt else None

    async def cancel(self, attempt_id: str) -> bool:
        async with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None:
                return False
            attempt.generation += 1
            if attempt.status not in TERMINAL_STATUSES:
                attempt.status = "cancelled"
                attempt.error = ""
            attempt.qr_content = ""
            attempt._transaction = ""
            task = attempt._task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # A task can be cancelled before its coroutine body ever starts, in which case
        # its `finally` block cannot close the freshly-created client. Claim it here;
        # when the poller did run, it already replaced this field with None.
        async with self._lock:
            client, attempt._client = attempt._client, None
        if client is not None:
            await client.aclose()
        return True

    async def cancel_for_account(self, account_id: str) -> int:
        """Cancel every in-flight reauthentication attempt bound to one account."""
        account_id = account_id.strip()
        async with self._lock:
            ids = [
                attempt_id
                for attempt_id, attempt in self._attempts.items()
                if attempt.reauth_account_id == account_id
                and attempt.status not in TERMINAL_STATUSES
            ]
        for attempt_id in ids:
            await self.cancel(attempt_id)
        return len(ids)

    async def aclose(self) -> None:
        async with self._lock:
            ids = list(self._attempts)
        for attempt_id in ids:
            await self.cancel(attempt_id)
        async with self._lock:
            self._attempts.clear()

    async def _poll(self, attempt: QrAttempt) -> None:
        generation = attempt.generation
        try:
            while True:
                if generation != attempt.generation or attempt.status in TERMINAL_STATUSES:
                    return
                if time.time() >= attempt.expires_at:
                    self._terminal(attempt, "expired")
                    return

                try:
                    result = await attempt._client.get_qrcode_status(attempt._transaction)  # type: ignore[union-attr]
                except asyncio.CancelledError:
                    raise
                except (IlinkTransportError, IlinkProtocolError, ValueError):
                    # A transient status failure does not overlap or spawn another poll. Keep
                    # retrying until TTL; no response/body/transaction is reflected publicly.
                    await self._sleep_until_next(attempt, generation)
                    continue

                if result.status == "scaned":
                    attempt.status = "scanned"
                elif result.status == "expired":
                    self._terminal(attempt, "expired")
                    return
                elif result.status == "confirmed":
                    if attempt.reauth_account_id and result.account_id != attempt.reauth_account_id:
                        self._terminal(attempt, "failed", "reauth_account_mismatch")
                        return
                    try:
                        base_url = validate_base_url(result.base_url or DEFAULT_BASE_URL)
                        profile = save_confirmation(
                            self._secrets,
                            account_id=result.account_id,
                            bot_token=result.bot_token,
                            base_url=base_url,
                            user_id=result.user_id,
                            display_name=result.user_id or result.account_id,
                        )
                    except (IlinkTransportError, ProfileError, ValueError):
                        self._terminal(attempt, "failed", "invalid_confirmation")
                        return
                    attempt.account_id = profile.account_id
                    attempt.display_name = profile.display_name or profile.user_id
                    self._terminal(attempt, "confirmed")
                    if self._on_confirm is not None:
                        try:
                            callback_result = self._on_confirm(profile.account_id)
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception:
                            # The credentials are already saved. A best-effort hot refresh may
                            # fail without turning a successful scan into a misleading failure.
                            pass
                    return

                await self._sleep_until_next(attempt, generation)
        except asyncio.CancelledError:
            return
        finally:
            attempt.qr_content = ""
            attempt._transaction = ""
            client, attempt._client = attempt._client, None
            if client is not None:
                await client.aclose()

    async def _sleep_until_next(self, attempt: QrAttempt, generation: int) -> None:
        remaining = attempt.expires_at - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(self._poll_interval, remaining))
        if generation != attempt.generation:
            raise asyncio.CancelledError

    @staticmethod
    def _terminal(attempt: QrAttempt, status: str, error: str = "") -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError("invalid terminal QR status")
        attempt.status = status
        attempt.error = error
        attempt.qr_content = ""
        attempt._transaction = ""

    async def _prune(self) -> None:
        cutoff = time.time() - self._terminal_retention
        async with self._lock:
            stale = [
                attempt_id
                for attempt_id, attempt in self._attempts.items()
                if attempt.status in TERMINAL_STATUSES and attempt.expires_at < cutoff
            ]
            for attempt_id in stale:
                self._attempts.pop(attempt_id, None)
