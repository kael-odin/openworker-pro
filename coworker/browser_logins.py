"""Browser login-state registry — persists which sites have captured login sessions.

Mirrors the prefs-persisted store pattern of ``PluginRegistry``. The registry is the
source of truth for "which site's login state do we have on disk" — it records each
login's id, entry URL, label, capture mode (Playwright storageState vs pasted cookies),
the relative path to the persisted state file, and whether the capture succeeded.

Stored under the ``browser_logins`` key of the manager prefs dict. State files live
under ``state_dir()/browser_profiles/<id>/`` (storageState.json or cookies.json).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def make_id(url: str) -> str:
    """Derive a filesystem-safe, stable id from a login URL.

    ``https://github.com/login`` → ``github-com``; ``https://app.slack.com`` →
    ``app-slack-com``. Two different entry paths on the same host collapse to the
    same id (a site has one login session), which is the intended one-profile-per-site
    behaviour. Falls back to a sanitized label-ish slug when the URL has no host.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        # No host (e.g. malformed input) — slugify the raw string as a last resort.
        raw = url.lower()
    else:
        raw = host
    slug = _SLUG_RE.sub("-", raw).strip("-")
    return slug or "site"


def safe_id(login_id: str) -> str:
    """Reject path traversal in a login id (it becomes a folder name under browser_profiles/)."""
    if not login_id or not isinstance(login_id, str):
        raise ValueError("empty login id")
    if ".." in login_id.split("/") or login_id.startswith("."):
        raise ValueError(f"invalid login id: {login_id!r}")
    if "/" in login_id or "\\" in login_id:
        raise ValueError(f"login id must not contain path separators: {login_id!r}")
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in login_id):
        raise ValueError(f"invalid login id: {login_id!r}")
    return login_id


@dataclass
class BrowserLoginEntry:
    """One captured (or pending) browser login session."""

    id: str
    url: str = ""                # the entry URL the user logs in at, e.g. https://github.com/login
    label: str = ""              # human-readable site name, e.g. "GitHub"
    storage_state_path: str = ""  # relative to state_dir(), e.g. browser_profiles/github-com/storageState.json
    cookie_path: str = ""         # relative to state_dir(), e.g. browser_profiles/github-com/cookies.json
    mode: str = "playwright"      # "playwright" (storageState) | "cookies" (pasted cookie JSON)
    has_state: bool = False       # whether a capture has succeeded and the state file exists
    captured_at: str = ""         # ISO timestamp of the last successful capture
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "label": self.label,
            "storage_state_path": self.storage_state_path,
            "cookie_path": self.cookie_path,
            "mode": self.mode,
            "has_state": self.has_state,
            "captured_at": self.captured_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BrowserLoginEntry":
        return cls(
            id=str(d.get("id") or ""),
            url=str(d.get("url") or ""),
            label=str(d.get("label") or ""),
            storage_state_path=str(d.get("storage_state_path") or ""),
            cookie_path=str(d.get("cookie_path") or ""),
            mode=str(d.get("mode") or "playwright"),
            has_state=bool(d.get("has_state") or False),
            captured_at=str(d.get("captured_at") or ""),
            notes=str(d.get("notes") or ""),
        )


class BrowserLoginRegistry:
    """Persists the set of browser logins under the ``browser_logins`` prefs key.

    The caller owns the prefs dict + save callback (manager._load_prefs / _save_prefs);
    this class mutates the dict in place and calls ``save()``.
    """

    def __init__(self, prefs: dict[str, Any], save: Callable[[], None]) -> None:
        self._prefs = prefs
        self._save = save

    def _raw(self) -> list[dict[str, Any]]:
        raw = self._prefs.get("browser_logins")
        if not isinstance(raw, list):
            return []
        return raw

    def _write(self, logins: list[BrowserLoginEntry]) -> None:
        self._prefs["browser_logins"] = [l.to_dict() for l in logins]
        self._save()

    def list(self) -> list[BrowserLoginEntry]:
        return [BrowserLoginEntry.from_dict(d) for d in self._raw()]

    def get(self, login_id: str) -> Optional[BrowserLoginEntry]:
        for l in self.list():
            if l.id == login_id:
                return l
        return None

    def has(self, login_id: str) -> bool:
        return self.get(login_id) is not None

    def add(self, entry: BrowserLoginEntry) -> BrowserLoginEntry:
        """Add or overwrite a login entry (re-adding a site overwrites)."""
        logins = self.list()
        logins = [l for l in logins if l.id != entry.id]
        logins.append(entry)
        self._write(logins)
        return entry

    def update(self, login_id: str, changes: dict[str, Any]) -> Optional[BrowserLoginEntry]:
        logins = self.list()
        updated = None
        for l in logins:
            if l.id == login_id:
                if "url" in changes:
                    l.url = str(changes["url"])
                if "label" in changes:
                    l.label = str(changes["label"])
                if "storage_state_path" in changes:
                    l.storage_state_path = str(changes["storage_state_path"])
                if "cookie_path" in changes:
                    l.cookie_path = str(changes["cookie_path"])
                if "mode" in changes:
                    l.mode = str(changes["mode"])
                if "has_state" in changes:
                    l.has_state = bool(changes["has_state"])
                if "captured_at" in changes:
                    l.captured_at = str(changes["captured_at"])
                if "notes" in changes:
                    l.notes = str(changes["notes"])
                updated = l
                break
        if updated is None:
            return None
        self._write(logins)
        return updated

    def remove(self, login_id: str) -> bool:
        logins = self.list()
        target = next((l for l in logins if l.id == login_id), None)
        if target is None:
            return False
        self._write([l for l in logins if l.id != login_id])
        return True


def match_login_for_url(url: str, logins: list[BrowserLoginEntry]) -> Optional[BrowserLoginEntry]:
    """Find the login entry whose host matches the given URL, preferring one with captured state.

    Used by ``browser_open_url`` to inject ``storage_state`` when the agent navigates
    to a site whose login session we already hold. Host match (not full-URL match)
    so a login captured at ``github.com/login`` applies to ``github.com/anything``.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return None
    candidates = [l for l in logins if (urlsplit(l.url).hostname or "").lower() == host]
    if not candidates:
        return None
    # Prefer a login that actually has captured state; otherwise the first match.
    with_state = [l for l in candidates if l.has_state]
    return (with_state[0] if with_state else candidates[0])
