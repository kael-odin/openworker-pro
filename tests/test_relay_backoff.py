"""Relay reconnect backoff (P1-06): exponential + jitter, capped, reset on success.

These tests target the backoff delay computation directly, independent of the
FakeTransport frame harness (whose event-loop timing is flaky in some envs).
"""

from __future__ import annotations

import asyncio

import pytest

from coworker.connectors import relay_client
from coworker.connectors.relay_client import RelayHub


class _FakeTransport:
    """Open succeeds or fails based on ``fail``; recv blocks forever (no frames)."""

    def __init__(self, *, fail: bool = False):
        self._fail = fail
        self.opened = False

    async def open(self):
        if self._fail:
            raise ConnectionError("simulated connect failure")
        self.opened = True

    async def recv(self):
        await asyncio.Event().wait()  # block until cancelled

    async def close(self):
        pass


def _hub(monkeypatch, *, reconnect_delay=2.0, jitter=0.5):
    """A RelayHub whose transport always fails to open, with deterministic jitter."""
    monkeypatch.setattr(relay_client, "_rand_jitter", lambda: jitter)
    calls: list[float] = []

    real_sleep = asyncio.sleep

    async def capture_sleep(delay):
        calls.append(delay)
        await real_sleep(0)  # don't actually wait

    monkeypatch.setattr(relay_client.asyncio, "sleep", capture_sleep)

    hub = RelayHub(
        "wss://relay.test/ws",
        token_provider=lambda: "jwt",
        transport_factory=lambda: _FakeTransport(fail=True),
        reconnect_delay=reconnect_delay,
    )
    return hub, calls


async def test_backoff_grows_exponentially(monkeypatch):
    hub, sleeps = _hub(monkeypatch, reconnect_delay=2.0, jitter=0.5)
    # jitter=0.5 → multiplier = 0.75 + 0.5*0.5 = 1.0 (deterministic, no jitter).
    hub._consecutive_failures = 0
    for _ in range(4):
        await hub._reconnect()
    # delays: 2, 4, 8, 16 (2^0..2^3 * base, capped at 30)
    assert sleeps == [2.0, 4.0, 8.0, 16.0]


async def test_backoff_caps_at_30s(monkeypatch):
    hub, sleeps = _hub(monkeypatch, reconnect_delay=2.0, jitter=0.5)
    hub._consecutive_failures = 10  # well past the cap exponent
    await hub._reconnect()
    await hub._reconnect()
    assert all(d <= 30.0 for d in sleeps)
    # 2^6 * 2 = 128 → capped to 30, * 1.0 jitter factor = 30
    assert sleeps == [30.0, 30.0]


async def test_backoff_zero_delay_skips_sleep(monkeypatch):
    """Tests pass reconnect_delay=0.0 for instant reconnects — no sleep at all."""
    hub, sleeps = _hub(monkeypatch, reconnect_delay=0.0)
    await hub._reconnect()
    assert sleeps == []


async def test_failure_counter_increments_on_failed_reconnect(monkeypatch):
    hub, _ = _hub(monkeypatch, reconnect_delay=2.0, jitter=0.5)
    assert hub._consecutive_failures == 0
    await hub._reconnect()  # transport.open() raises
    assert hub._consecutive_failures == 1
    await hub._reconnect()
    assert hub._consecutive_failures == 2


async def test_failure_counter_resets_on_successful_start(monkeypatch):
    hub, _ = _hub(monkeypatch, reconnect_delay=2.0)
    hub._consecutive_failures = 5
    # Override transport to succeed on open.
    hub._transport_factory = lambda: _FakeTransport(fail=False)
    ok = await hub.start()
    try:
        assert ok is True
        assert hub._consecutive_failures == 0
    finally:
        await hub.stop()
