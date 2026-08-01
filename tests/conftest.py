"""Shared pytest fixtures.

`fake_slack` boots the in-process FakeSlack harness on an ephemeral port and points the Slack
adapter at it via `SLACK_API_URL`, so the real `SlackAdapter` / `slack_bolt` stack runs
end-to-end with no network, tokens, or the Slack app console. See
`coworker.testing.fake_slack` and `platform/docs/FAKE-SLACK-SPEC.md`.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from coworker.testing.fake_slack import FakeSlack


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """EVERY test gets an isolated SecretStore/state dir. Without this, any test that builds
    a SessionManager reads the developer's real machine-global state — including their cloud
    sign-in, which made test session creation emit REAL telemetry to prod (found 2026-07-03
    as burst noise in the ocw-connect-telemetry-events table)."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "coworker-state"))
    monkeypatch.delenv("COWORKER_API_TOKEN", raising=False)
    # P1-01 fail-closed: a missing sidecar token now rejects authenticated requests in
    # production. The test suite builds tokenless TestClients everywhere; treat the whole
    # run as the explicit local-dev opt-in so those clients keep working. Tests that verify
    # the gated path (test_sidecar_token_gates_rest_and_websockets) set a real token.
    monkeypatch.setenv("COWORKER_INSECURE_LOCAL_DEV", "1")


@pytest_asyncio.fixture
async def fake_slack(monkeypatch):
    """A running FakeSlack control object; `SLACK_API_URL` is set to it for the test's duration."""
    fake = FakeSlack()
    await fake.start()
    monkeypatch.setenv("SLACK_API_URL", fake.api_url)
    try:
        yield fake
    finally:
        await fake.stop()
