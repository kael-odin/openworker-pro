"""Browser OAuth for remote MCP servers (OAuth 2.1 + PKCE + Dynamic Client Registration).

The official SDK's `OAuthClientProvider` drives the whole spec flow — protected-resource
metadata discovery, DCR, PKCE, token refresh — as an httpx auth plugged into the
streamable-HTTP transport. We supply its three integration points:

  - token persistence  → the SecretStore (profile `mcp-oauth:<server>`; 0600 file,
    never the mcp.json config, which is plain text and paste-shareable)
  - redirect           → open the system browser at the authorize URL
  - callback           → the sidecar's loopback `GET /mcp/oauth/callback` resolves a
    single-slot pending future (one interactive sign-in at a time — the flow is
    user-driven, so concurrency is meaningless)

DCR means there is no client id/secret registered anywhere up front — nothing for the
ocw-connect broker to hold, so unlike the managed connectors this flow is fully local.
First server: Granola (https://mcp.granola.ai/mcp).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any, Optional

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from ..secrets import SecretStore

logger = logging.getLogger(__name__)

PROFILE_PREFIX = "mcp-oauth:"
CALLBACK_PATH = "/mcp/oauth/callback"
# How long the connect waits for the user to finish the browser sign-in.
FLOW_TIMEOUT_SECONDS = 300

CLIENT_NAME = "OpenWorker"


def redirect_base() -> str:
    """The sidecar's own loopback origin — the DCR-registered redirect must match it."""
    port = os.environ.get("COWORKER_PORT") or "8765"
    return f"http://127.0.0.1:{port}"


def _profile(name: str) -> str:
    return PROFILE_PREFIX + name


class SecretStoreTokenStorage(TokenStorage):
    """SDK TokenStorage over our SecretStore: one profile per server holding the token
    set and the DCR-issued client registration (re-used across sign-ins)."""

    def __init__(self, server_name: str, secrets: SecretStore) -> None:
        self._name = server_name
        self._secrets = secrets

    def _data(self) -> dict[str, Any]:
        return self._secrets.get(_profile(self._name)) or {}

    def _merge(self, patch: dict[str, Any]) -> None:
        self._secrets.put(_profile(self._name), {**self._data(), **patch})

    async def get_tokens(self) -> Optional[OAuthToken]:
        raw = self._data().get("tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._merge({"tokens": tokens.model_dump(mode="json", exclude_none=True)})

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        raw = self._data().get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:
            return None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._merge({"client_info": info.model_dump(mode="json", exclude_none=True)})


class InteractiveAuthRequired(RuntimeError):
    """The server wants a browser sign-in, but this context must not open one.

    Interactive OAuth (browser + loopback wait) is an explicit-connect-only
    privilege: a background context that hit this — an engine turn, a tools
    listing — raises instead, and the caller skips the server. Without this, a
    server whose refresh token the vendor rejected (Atlassian rotates them
    aggressively) would hijack the user's browser from ANY code path that
    touched it — owner-hit 2026-07-20: an authorize page opened at app launch.
    """


def is_auth_required(exc: BaseException) -> bool:
    """True if InteractiveAuthRequired is anywhere in the exception tree — the SDK
    transport runs in anyio task groups, so it often arrives wrapped in an
    ExceptionGroup (or chained as a cause) rather than bare."""
    if isinstance(exc, InteractiveAuthRequired):
        return True
    for sub in getattr(exc, "exceptions", None) or []:  # ExceptionGroup
        if is_auth_required(sub):
            return True
    cause = exc.__cause__ or exc.__context__
    return is_auth_required(cause) if cause is not None else False


# -- single-slot interactive flow ------------------------------------------------
_pending: Optional[asyncio.Future] = None
# The last authorize URL we sent the user to — surfaced over REST so the GUI can offer
# a "reopen sign-in page" link if the browser popup was lost.
last_authorize_url: Optional[str] = None
# The `state` bound to the current interactive flow. Always client-generated (P1-02):
# `_inject_state` guarantees the authorize URL carries a state even when the server/SDK
# omits one, so `deliver_callback` can always enforce an exact match. This is the loopback
# gate — without it any local caller could hit /mcp/oauth/callback with a bogus code and
# consume the single pending future, aborting the user's real sign-in. The SDK itself also
# re-checks state (mcp.client.auth.oauth2 compare_digest) server-side; both must pass.
_expected_state: Optional[str] = None


def _state_from_url(url: str) -> Optional[str]:
    """Pull the `state` query param out of an authorize URL (None if absent)."""
    from urllib.parse import parse_qs, urlsplit

    values = parse_qs(urlsplit(url).query).get("state")
    return values[0] if values else None


def deliver_callback(code: str, state: Optional[str]) -> bool:
    """Called by the loopback route. Resolves the waiting flow; False if none waits.

    A callback whose `state` doesn't match the pending flow's is ignored (returns False)
    WITHOUT consuming the pending future, so a stray/forged local hit can't abort a live
    sign-in — only the browser redirect carrying our state resolves it. The state is
    client-generated (P1-02): we never depend on the server to supply or echo one, so a
    server that omits state from its authorize URL still gets CSRF protection.
    """
    global _pending
    if _pending is None or _pending.done():
        return False
    if _expected_state is None or (
        state is None or not secrets.compare_digest(state, _expected_state)
    ):
        return False
    pending, _pending = _pending, None
    pending.set_result((code, state))
    return True


def _inject_state(url: str) -> str:
    """Ensure the authorize URL carries a client-generated state (P1-02).

    OAuth state is a CSRF guard that must always be present and bound to this flow — never
    dependent on whether the server/SDK chose to include one. If the URL already has a
    state, keep it (the SDK's compare_digest will also check it server-side). If not, we
    generate a fresh high-entropy state and append it; the loopback callback then enforces
    an exact match against `_expected_state`, so a forged callback without it is rejected.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "state" not in query or not query["state"]:
        query["state"] = secrets.token_urlsafe(32)
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _open_browser(url: str) -> None:
    global last_authorize_url, _expected_state
    url = _inject_state(url)
    last_authorize_url = url
    _expected_state = _state_from_url(url)
    import webbrowser

    logger.info("mcp oauth: opening browser for sign-in")
    await asyncio.get_running_loop().run_in_executor(None, webbrowser.open, url)


async def _refuse_browser(url: str) -> None:
    """Non-interactive redirect handler: never open a browser, but keep the URL so
    the GUI's "reopen sign-in page" affordance still works after the refusal."""
    global last_authorize_url
    last_authorize_url = url
    raise InteractiveAuthRequired(
        "sign-in required — reconnect this server from its page"
    )


async def _refuse_callback() -> tuple[str, Optional[str]]:
    raise InteractiveAuthRequired(
        "sign-in required — reconnect this server from its page"
    )


async def _wait_for_callback() -> tuple[str, Optional[str]]:
    global _pending, _expected_state
    if _pending is not None and not _pending.done():
        _pending.cancel()  # a stale flow lost its browser tab; the new one wins
    _pending = asyncio.get_running_loop().create_future()
    try:
        return await asyncio.wait_for(_pending, timeout=FLOW_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise RuntimeError(
            "sign-in timed out — the browser window was not completed in "
            f"{FLOW_TIMEOUT_SECONDS // 60} minutes"
        )
    finally:
        _pending = None
        _expected_state = None  # don't let this flow's state gate the next one


def build_auth(
    server_name: str,
    server_url: str,
    secrets: SecretStore,
    *,
    interactive: bool = True,
) -> OAuthClientProvider:
    """The httpx auth for one OAuth MCP server (pass as streamablehttp_client(auth=…)).

    `interactive=False` still uses stored tokens and silent refresh, but the moment
    the SDK wants a browser authorization it raises InteractiveAuthRequired instead
    of opening one — only explicit connect actions pass True.
    """
    metadata = OAuthClientMetadata.model_validate(
        {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_base() + CALLBACK_PATH],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            # Public client: DCR issues no secret a native app could keep anyway.
            "token_endpoint_auth_method": "none",
        }
    )
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=SecretStoreTokenStorage(server_name, secrets),
        redirect_handler=_open_browser if interactive else _refuse_browser,
        callback_handler=_wait_for_callback if interactive else _refuse_callback,
    )


def has_tokens(server_name: str, secrets: SecretStore) -> bool:
    return bool((secrets.get(_profile(server_name)) or {}).get("tokens"))


def sign_out(server_name: str, secrets: SecretStore) -> bool:
    """Forget tokens AND the DCR registration; next connect runs a fresh flow."""
    return secrets.delete(_profile(server_name))
