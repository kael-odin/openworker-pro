"""Browser login-state capture — two paths to persist a logged-in session.

1. ``capture_via_playwright`` — open a headed Playwright chromium window at the login
   URL, let the user log in manually, then on a confirm signal call
   ``context.storage_state(path=...)`` to dump cookies + localStorage to disk. This is
   the preferred path (full session fidelity).

2. ``capture_via_cookies`` — the user pastes a cookie JSON array (exported from a
   browser extension) which we persist verbatim. Used when Playwright isn't installed
   (the sidecar process often runs without it) or for headless servers.

Both write under ``state_dir()/browser_profiles/<id>/``. The registry records which
path succeeded and the relative path to the state file.

Playwright is an optional dependency (``pip install coworker[browser]``). Every entry
point here degrades gracefully: if Playwright can't be imported, capture_via_playwright
returns ``{ok: False, fallback: "cookies"}`` so the frontend can switch to paste mode.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .browser_logins import BrowserLoginEntry, safe_id


# Cache the Playwright-availability probe (import test) — it's process-stable.
_playwright_available: Optional[bool] = None
# The error message captured during the probe (None when the import succeeded or was
# never run). Surfaces *why* Playwright is unavailable so the GUI/manager can tell the
# user "playwright is installed but its native deps are broken" vs "not installed".
_playwright_probe_error: Optional[str] = None
_probe_lock = threading.Lock()


def try_playwright_available() -> bool:
    """True if ``playwright.sync_api`` can be imported. Cached after the first probe.

    The probe used to swallow every exception silently, so a Playwright install whose
    native browser deps were broken (greenlet/asyncio import-time failures, missing
    OS libraries) looked identical to "not installed" — the user got a "install
    playwright" hint that was already satisfied. The error is now captured into
    ``playwright_probe_error()`` and logged so the cause is diagnosable.
    """
    global _playwright_available, _playwright_probe_error
    if _playwright_available is not None:
        return _playwright_available
    with _probe_lock:
        if _playwright_available is not None:
            return _playwright_available
        import logging

        log = logging.getLogger("coworker.browser_login_capture")
        try:
            import playwright.sync_api  # noqa: F401

            _playwright_available = True
            _playwright_probe_error = None
        except ImportError as exc:
            # The common, expected case: the optional dep isn't installed. Debug-level
            # is enough — the GUI already nudges the user to install it.
            _playwright_available = False
            _playwright_probe_error = str(exc)
            log.debug("playwright not installed: %s", exc)
        except Exception as exc:
            # Installed but broken — native deps, a partial install, or an incompatible
            # greenlet/anyio version. This is the case that was silently swallowed; warn
            # so it shows up in logs without the user having to guess.
            _playwright_available = False
            _playwright_probe_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "playwright is installed but unusable (%s). "
                "Try `python -m playwright install chromium` or reinstall playwright.",
                _playwright_probe_error,
            )
    return _playwright_available


def playwright_probe_error() -> Optional[str]:
    """The error captured by the last ``try_playwright_available`` probe, or None.

    None when Playwright imported cleanly. Useful for surfacing a precise diagnostic
    (e.g. "playwright is installed but its native deps failed to load") instead of a
    generic "install playwright" message.
    """
    return _playwright_probe_error


def profile_dir(profiles_root: Path, login_id: str) -> Path:
    """The per-login profile directory: ``<profiles_root>/<safe_id>/``."""
    return Path(profiles_root) / safe_id(login_id)


def storage_state_rel(login_id: str) -> str:
    """Relative-to-state_dir path of the Playwright storageState dump."""
    return f"browser_profiles/{safe_id(login_id)}/storageState.json"


def cookies_rel(login_id: str) -> str:
    """Relative-to-state_dir path of the pasted cookies JSON."""
    return f"browser_profiles/{safe_id(login_id)}/cookies.json"


# -- Playwright headed capture --------------------------------------------------


class _PlaywrightCaptureSession:
    """A live headed Playwright window awaiting the user to finish logging in.

    Captures are interactive: ``begin`` opens the window + navigates to the login URL,
    then the caller (frontend) must call ``confirm`` (persist storageState) or
    ``cancel`` (close without persisting). One session at a time per process — the
    browser automation controller is a singleton too.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._login_id: Optional[str] = None

    def begin(self, url: str, login_id: str) -> dict[str, Any]:
        with self._lock:
            # If a previous capture was left open, tear it down first.
            self._teardown_locked()
            if not try_playwright_available():
                return {"ok": False, "fallback": "cookies"}
            try:
                from playwright.sync_api import sync_playwright

                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=False)
                self._context = self._browser.new_context(
                    viewport={"width": 1280, "height": 900}
                )
                self._page = self._context.new_page()
                self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                self._login_id = login_id
                return {"ok": True, "mode": "playwright", "id": login_id, "url": self._page.url}
            except Exception as exc:
                self._teardown_locked()
                # A failed launch (e.g. no display / no chromium binary) falls back to cookies.
                return {"ok": False, "fallback": "cookies", "error": str(exc)}

    def confirm(self, profiles_root: Path, login_id: str) -> dict[str, Any]:
        """Persist the current context's storageState to disk, then close."""
        with self._lock:
            if self._context is None or self._login_id != login_id:
                return {"ok": False, "error": "no active capture session for this login"}
            try:
                d = profile_dir(profiles_root, login_id)
                d.mkdir(parents=True, exist_ok=True)
                state_path = d / "storageState.json"
                self._context.storage_state(path=str(state_path))
                captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                return {
                    "ok": True,
                    "mode": "playwright",
                    "storage_state_path": storage_state_rel(login_id),
                    "captured_at": captured_at,
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            finally:
                self._teardown_locked()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            self._teardown_locked()
            return {"ok": True}

    def is_active(self) -> bool:
        with self._lock:
            return self._context is not None

    def state(self) -> dict[str, Any]:
        """Live snapshot of the capture window (url/title), for the frontend to poll."""
        with self._lock:
            if self._page is None:
                return {"active": False}
            try:
                return {
                    "active": True,
                    "login_id": self._login_id,
                    "url": self._page.url,
                    "title": self._page.title(),
                }
            except Exception:
                return {"active": True, "login_id": self._login_id, "url": "", "title": ""}

    def _teardown_locked(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self._pw = None
            self._browser = None
            self._context = None
            self._page = None
            self._login_id = None


# Process-singleton — only one headed capture window at a time.
_CAPTURE_SESSION = _PlaywrightCaptureSession()


def begin_playwright_capture(url: str, login_id: str) -> dict[str, Any]:
    """Open a headed browser at the login URL. Returns {ok} or {ok:False, fallback:'cookies'}."""
    return _CAPTURE_SESSION.begin(url, login_id)


def confirm_playwright_capture(profiles_root: Path, login_id: str) -> dict[str, Any]:
    """User signals 'I'm logged in' — dump storageState, close the window."""
    return _CAPTURE_SESSION.confirm(profiles_root, login_id)


def cancel_playwright_capture() -> dict[str, Any]:
    """User cancels — close the window without persisting."""
    return _CAPTURE_SESSION.cancel()


def capture_session_state() -> dict[str, Any]:
    """Live state of the capture window, for polling."""
    return _CAPTURE_SESSION.state()


# -- Cookie paste capture -------------------------------------------------------


def capture_via_cookies(
    login_id: str, cookie_json: str, profiles_root: Path
) -> dict[str, Any]:
    """Validate + persist a pasted cookie JSON array for the login.

    The JSON must be a list of cookie objects (the Playwright/CDP cookie shape). We
    don't validate individual cookie fields strictly — the browser will reject
    malformed cookies at load time — but we do ensure it parses as JSON.
    """
    if not cookie_json or not cookie_json.strip():
        return {"ok": False, "error": "empty cookie JSON"}
    try:
        parsed = json.loads(cookie_json)
    except Exception as exc:
        return {"ok": False, "error": f"invalid cookie JSON: {exc}"}
    if not isinstance(parsed, list):
        return {"ok": False, "error": "cookie JSON must be an array of cookie objects"}
    try:
        d = profile_dir(profiles_root, login_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cookies.json").write_text(json.dumps(parsed, indent=2), encoding="utf-8")
        captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {
            "ok": True,
            "mode": "cookies",
            "cookie_path": cookies_rel(login_id),
            "captured_at": captured_at,
            "count": len(parsed),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def cookies_to_storage_state(cookies_path: Path, storage_state_path: Path) -> bool:
    """Convert a pasted cookies.json into a Playwright storageState.json.

    Playwright's ``new_context(storage_state=...)`` accepts either a path to a
    storageState file (``{cookies: [...], origins: [...]}``) or a cookie list. We
    normalize pasted cookies into the storageState shape so ``browser_open_url`` has a
    single code path. Returns True on success.
    """
    try:
        cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
        if not isinstance(cookies, list):
            return False
        state = {"cookies": cookies, "origins": []}
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        storage_state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# -- Expiry inspection ----------------------------------------------------------


# A session cookie (expires == -1 / 0 / absent) never expires on its own — it dies
# when the browser closes. For login-state purposes we treat those as "no expiry
# signal" rather than "expired". Only an explicit future/past unix timestamp counts.
_EXPIRING_SOON_SECONDS = 3 * 24 * 3600  # within 3 days → "expiring soon"


def _cookie_expires(cookie: Any) -> Optional[float]:
    """Extract a cookie's expiry as a unix timestamp (seconds), or None for session cookies.

    Playwright/CDP cookies carry ``expires`` as a float unix timestamp; ``-1`` means
    session cookie. Pasted cookies may use ``expires`` or ``expiry`` (the JSON shape
    varies by exporter). Returns None when there's no real expiry signal.
    """
    if not isinstance(cookie, dict):
        return None
    val = cookie.get("expires", cookie.get("expiry"))
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    # -1 (Playwright) or 0 (some exporters) → session cookie, no real expiry.
    if val <= 0:
        return None
    return val


def inspect_login_expiry(entry: BrowserLoginEntry, state_dir: Path) -> dict[str, Any]:
    """Inspect a login's persisted state for the nearest cookie expiry.

    Returns ``{status, expires_at?}`` where status is one of:
      - ``"no_state"``     — nothing captured yet
      - ``"session"``      — only session cookies (no expiry signal); likely valid until logout
      - ``"valid"``        — has a real expiry in the future
      - ``"expiring"``     — expires within ``_EXPIRING_SOON_SECONDS``
      - ``"expired"``      — the latest expiry has already passed

    ``expires_at`` (ISO UTC) is included when a real expiry exists. We report the
    *latest* cookie expiry as the session's effective expiry — that's the cookie
    keeping the login alive (e.g. a remember-me token), and shorter-lived cookies
    refresh on use.
    """
    if not entry.has_state:
        return {"status": "no_state"}

    # Prefer the storageState file (Playwright path); fall back to cookies.json.
    state_file: Optional[Path] = None
    if entry.storage_state_path:
        p = Path(state_dir) / entry.storage_state_path
        if p.is_file():
            state_file = p
    if state_file is None and entry.cookie_path:
        p = Path(state_dir) / entry.cookie_path
        if p.is_file():
            state_file = p
    if state_file is None:
        return {"status": "no_state"}

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "no_state"}

    # storageState shape: {cookies: [...], origins: [...]}; pasted cookies: [...] .
    cookies = data["cookies"] if isinstance(data, dict) else data
    if not isinstance(cookies, list):
        return {"status": "no_state"}

    expiries = [e for e in (_cookie_expires(c) for c in cookies) if e is not None]
    if not expiries:
        return {"status": "session"}

    latest = max(expiries)
    now = time.time()
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(latest))
    if latest <= now:
        return {"status": "expired", "expires_at": expires_at}
    if latest - now <= _EXPIRING_SOON_SECONDS:
        return {"status": "expiring", "expires_at": expires_at}
    return {"status": "valid", "expires_at": expires_at}
