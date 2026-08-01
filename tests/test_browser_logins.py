"""Browser login-state registry + capture (批次 E5).

Tests the login registry (prefs persistence), the id/slug derivation, the path-traversal
guard, the cookie-paste capture path (no network), the Playwright availability probe,
the URL→login host matcher, and the cookies→storageState conversion. The Playwright
headed capture path is exercised via a mock (no real browser).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coworker.browser_logins import (
    BrowserLoginEntry,
    BrowserLoginRegistry,
    make_id,
    match_login_for_url,
    safe_id,
)
from coworker import browser_login_capture as capture


# -- Registry (prefs persistence, CRUD) -----------------------------------------


def _prefs_and_registry():
    prefs: dict = {}
    save = lambda: None  # noqa: E731 — in-memory; save is a no-op
    return prefs, BrowserLoginRegistry(prefs, save)


def test_registry_empty_when_uninitialized():
    prefs, reg = _prefs_and_registry()
    assert reg.list() == []


def test_registry_add_and_get():
    _, reg = _prefs_and_registry()
    entry = BrowserLoginEntry(id="github-com", url="https://github.com/login", label="GitHub")
    reg.add(entry)
    got = reg.get("github-com")
    assert got is not None
    assert got.url == "https://github.com/login"
    assert got.label == "GitHub"
    assert got.has_state is False


def test_registry_add_overwrites_same_id():
    _, reg = _prefs_and_registry()
    reg.add(BrowserLoginEntry(id="github-com", url="https://github.com/login", label="GitHub"))
    reg.add(BrowserLoginEntry(id="github-com", url="https://github.com/login", label="GitHub Inc.", has_state=True))
    logins = reg.list()
    assert len(logins) == 1
    assert logins[0].label == "GitHub Inc."
    assert logins[0].has_state is True


def test_registry_update():
    _, reg = _prefs_and_registry()
    reg.add(BrowserLoginEntry(id="github-com", url="https://github.com/login", label="GitHub"))
    updated = reg.update("github-com", {"has_state": True, "captured_at": "2026-07-31T00:00:00Z"})
    assert updated is not None
    assert updated.has_state is True
    assert updated.captured_at == "2026-07-31T00:00:00Z"
    assert reg.get("github-com").has_state is True


def test_registry_remove():
    _, reg = _prefs_and_registry()
    reg.add(BrowserLoginEntry(id="github-com", url="https://github.com/login", label="GitHub"))
    assert reg.remove("github-com") is True
    assert reg.list() == []
    assert reg.remove("github-com") is False  # already gone


# -- id derivation + path traversal guard ---------------------------------------


def test_make_id_from_url():
    assert make_id("https://github.com/login") == "github-com"
    assert make_id("https://app.slack.com") == "app-slack-com"
    assert make_id("https://example.com:8080/path") == "example-com"


def test_make_id_no_host_falls_back():
    # No host → slugify the raw string; must be non-empty.
    assert make_id("not a url") == "not-a-url"


def test_safe_id_rejects_traversal():
    with pytest.raises(ValueError):
        safe_id("../etc")
    with pytest.raises(ValueError):
        safe_id("foo/bar")
    with pytest.raises(ValueError):
        safe_id("foo\\bar")
    with pytest.raises(ValueError):
        safe_id("foo!bar")  # invalid char
    with pytest.raises(ValueError):
        safe_id("")


def test_safe_id_accepts_clean():
    assert safe_id("github-com") == "github-com"
    assert safe_id("app-slack-com") == "app-slack-com"


# -- match_login_for_url (host matching) ----------------------------------------


def test_match_login_by_host():
    logins = [
        BrowserLoginEntry(id="github-com", url="https://github.com/login", has_state=True),
        BrowserLoginEntry(id="slack-com", url="https://slack.com", has_state=False),
    ]
    m = match_login_for_url("https://github.com/dashboard", logins)
    assert m is not None
    assert m.id == "github-com"


def test_match_prefers_has_state():
    logins = [
        BrowserLoginEntry(id="github-com", url="https://github.com/login", has_state=False),
        BrowserLoginEntry(id="github-com-2", url="https://github.com/login", has_state=True),
    ]
    m = match_login_for_url("https://github.com/anything", logins)
    assert m is not None
    assert m.has_state is True


def test_match_no_match_returns_none():
    logins = [BrowserLoginEntry(id="github-com", url="https://github.com/login")]
    assert match_login_for_url("https://other.com", logins) is None
    assert match_login_for_url("not-a-url", logins) is None


# -- Cookie paste capture -------------------------------------------------------


def test_capture_via_cookies_valid(tmp_path: Path):
    cookie_json = json.dumps([{"name": "session", "value": "abc", "domain": ".github.com", "path": "/"}])
    result = capture.capture_via_cookies("github-com", cookie_json, tmp_path)
    assert result["ok"] is True
    assert result["mode"] == "cookies"
    assert result["cookie_path"] == "browser_profiles/github-com/cookies.json"
    assert result["count"] == 1
    # The file landed on disk.
    assert (tmp_path / "github-com" / "cookies.json").is_file()


def test_capture_via_cookies_invalid_json(tmp_path: Path):
    result = capture.capture_via_cookies("github-com", "not json", tmp_path)
    assert result["ok"] is False
    assert "invalid cookie JSON" in result["error"]


def test_capture_via_cookies_must_be_array(tmp_path: Path):
    result = capture.capture_via_cookies("github-com", json.dumps({"name": "session"}), tmp_path)
    assert result["ok"] is False
    assert "array" in result["error"]


def test_capture_via_cookies_empty(tmp_path: Path):
    result = capture.capture_via_cookies("github-com", "", tmp_path)
    assert result["ok"] is False


def test_cookies_to_storage_state(tmp_path: Path):
    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(json.dumps([{"name": "s", "value": "v", "domain": ".x.com", "path": "/"}]), encoding="utf-8")
    state_path = tmp_path / "storageState.json"
    assert capture.cookies_to_storage_state(cookies_path, state_path) is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["cookies"] == [{"name": "s", "value": "v", "domain": ".x.com", "path": "/"}]
    assert state["origins"] == []


# -- Playwright availability probe ----------------------------------------------


def test_try_playwright_available_returns_bool():
    # The probe is cached; just assert it returns a bool and doesn't raise.
    assert isinstance(capture.try_playwright_available(), bool)


# -- Capture session (mocked Playwright) ----------------------------------------


def test_begin_capture_without_playwright_falls_back(monkeypatch):
    monkeypatch.setattr(capture, "_playwright_available", False)
    result = capture.begin_playwright_capture("https://github.com/login", "github-com")
    assert result["ok"] is False
    assert result["fallback"] == "cookies"


# -- Expiry inspection ----------------------------------------------------------


import time as _time  # noqa: E402


def _entry_with_state(login_id: str, rel_path: str) -> BrowserLoginEntry:
    return BrowserLoginEntry(
        id=login_id, url=f"https://{login_id.replace('-', '.')}/login",
        label=login_id, storage_state_path=rel_path, has_state=True,
    )


def test_expiry_no_state_when_not_captured(tmp_path: Path):
    entry = BrowserLoginEntry(id="x-com", has_state=False)
    assert capture.inspect_login_expiry(entry, tmp_path) == {"status": "no_state"}


def test_expiry_no_state_when_file_missing(tmp_path: Path):
    entry = _entry_with_state("x-com", "browser_profiles/x-com/storageState.json")
    assert capture.inspect_login_expiry(entry, tmp_path) == {"status": "no_state"}


def test_expiry_session_cookies(tmp_path: Path):
    # Cookies with expires=-1 are session cookies → no real expiry signal.
    rel = "browser_profiles/x-com/storageState.json"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(json.dumps({
        "cookies": [{"name": "s", "value": "v", "expires": -1}],
        "origins": [],
    }), encoding="utf-8")
    entry = _entry_with_state("x-com", rel)
    assert capture.inspect_login_expiry(entry, tmp_path) == {"status": "session"}


def test_expiry_valid_future(tmp_path: Path):
    future = _time.time() + 30 * 24 * 3600  # 30 days out
    rel = "browser_profiles/x-com/cookies.json"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(json.dumps([
        {"name": "s", "value": "v", "expires": future},
    ]), encoding="utf-8")
    entry = BrowserLoginEntry(id="x-com", cookie_path=rel, has_state=True)
    result = capture.inspect_login_expiry(entry, tmp_path)
    assert result["status"] == "valid"
    assert "expires_at" in result


def test_expiry_expiring_soon(tmp_path: Path):
    soon = _time.time() + 2 * 24 * 3600  # 2 days — within the 3-day window
    rel = "browser_profiles/x-com/cookies.json"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(json.dumps([
        {"name": "s", "value": "v", "expires": soon},
    ]), encoding="utf-8")
    entry = BrowserLoginEntry(id="x-com", cookie_path=rel, has_state=True)
    assert capture.inspect_login_expiry(entry, tmp_path)["status"] == "expiring"


def test_expiry_expired(tmp_path: Path):
    past = _time.time() - 3600  # an hour ago
    rel = "browser_profiles/x-com/cookies.json"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(json.dumps([
        {"name": "s", "value": "v", "expires": past},
    ]), encoding="utf-8")
    entry = BrowserLoginEntry(id="x-com", cookie_path=rel, has_state=True)
    result = capture.inspect_login_expiry(entry, tmp_path)
    assert result["status"] == "expired"
    assert "expires_at" in result


def test_expiry_uses_latest_cookie_expiry(tmp_path: Path):
    # A short-lived cookie (expired) + a long-lived one (valid) → the session is valid.
    past = _time.time() - 3600
    future = _time.time() + 30 * 24 * 3600
    rel = "browser_profiles/x-com/cookies.json"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(json.dumps([
        {"name": "short", "value": "v", "expires": past},
        {"name": "remember", "value": "v", "expires": future},
    ]), encoding="utf-8")
    entry = BrowserLoginEntry(id="x-com", cookie_path=rel, has_state=True)
    assert capture.inspect_login_expiry(entry, tmp_path)["status"] == "valid"


# -- Export / import (backup) ---------------------------------------------------

# Manager methods exercise the registry + state files together. We construct a minimal
# fake manager shell (only the attributes these methods touch) to avoid spinning up a
# full SessionManager.

from coworker.browser_logins import BrowserLoginEntry as _BLE  # noqa: E402


class _FakeManager:
    """Just enough surface for export/import_browser_logins to run against."""

    def __init__(self, prefs, base):
        self._prefs = prefs
        self.browser_logins = BrowserLoginRegistry(prefs, lambda: None)
        self._browser_profiles_dir = base / "browser_profiles"
        self._browser_profiles_dir.mkdir(parents=True, exist_ok=True)

    # Reuse the real methods on SessionManager via unbound-style call.
    from coworker.server.manager import SessionManager as _SM

    export_browser_logins = _SM.export_browser_logins
    import_browser_logins = _SM.import_browser_logins

    # state_dir is read via the module-level import; we shim it per-call by patching the
    # function used inside (state_dir from coworker.secrets). Simpler: monkeypatch in tests.


def _manager_with_login(tmp_path: Path) -> _FakeManager:
    prefs: dict = {}
    m = _FakeManager(prefs, tmp_path)
    entry = _BLE(
        id="github-com", url="https://github.com/login", label="GitHub",
        storage_state_path="browser_profiles/github-com/storageState.json",
        has_state=True, mode="playwright",
    )
    m.browser_logins.add(entry)
    state_dir = tmp_path / "browser_profiles" / "github-com"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storageState.json").write_text(
        json.dumps({"cookies": [{"name": "s", "value": "v"}], "origins": []}), encoding="utf-8"
    )
    return m


def test_export_includes_entries_and_files(tmp_path: Path, monkeypatch):
    m = _manager_with_login(tmp_path)
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    out = m.export_browser_logins()
    assert out["version"] == 1
    assert len(out["logins"]) == 1
    entry = out["logins"][0]
    assert entry["id"] == "github-com"
    assert "browser_profiles/github-com/storageState.json" in entry["_files"]
    assert "s" in entry["_files"]["browser_profiles/github-com/storageState.json"]


def test_import_restores_to_empty_registry(tmp_path: Path, monkeypatch):
    # Build an export payload from a populated manager, wipe it, then import.
    src = _manager_with_login(tmp_path)
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    payload = json.dumps(src.export_browser_logins())

    # Fresh manager (empty registry), same base dir.
    m2 = _FakeManager({}, tmp_path)
    r = m2.import_browser_logins(payload)
    assert r["ok"] is True
    assert r["imported"] == 1
    logins = m2.browser_logins.list()
    assert len(logins) == 1
    assert logins[0].id == "github-com"
    assert logins[0].has_state is True
    # The state file was restored to disk.
    assert (tmp_path / "browser_profiles" / "github-com" / "storageState.json").is_file()


def test_import_rejects_bad_json(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    m = _FakeManager({}, tmp_path)
    r = m.import_browser_logins("not json")
    assert r["ok"] is False
    assert "invalid" in r["error"].lower() or "json" in r["error"].lower()


def test_import_skips_traversal_rel_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    payload = json.dumps({"version": 1, "logins": [{
        "id": "evil-com", "url": "https://evil.com", "label": "Evil",
        "has_state": True, "_files": {"../../../escape.txt": "pwned"},
    }]})
    m = _FakeManager({}, tmp_path)
    r = m.import_browser_logins(payload)
    assert r["ok"] is True
    assert not (tmp_path.parent.parent / "escape.txt").exists()


def test_import_rejects_absolute_path_in_files(tmp_path: Path, monkeypatch):
    """An absolute path in _files must not escape the state dir — the canonical-filename
    filter discards it regardless of the supplied path."""
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    target = tmp_path.parent / "stolen.txt"
    payload = json.dumps({"version": 1, "logins": [{
        "id": "evil-com", "url": "https://evil.com", "label": "Evil",
        "has_state": True,
        "_files": {str(target): "exfiltrated"},
    }]})
    m = _FakeManager({}, tmp_path)
    r = m.import_browser_logins(payload)
    assert r["ok"]  # import succeeds but the malicious file is not written
    assert not target.exists()


def test_import_rejects_same_prefix_sibling(tmp_path: Path, monkeypatch):
    """String ``startswith`` can be bypassed by a sibling directory whose name shares a prefix
    with the state dir (e.g. ``state_dir_evil``). ``is_relative_to`` must reject it."""
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    # A path that resolves to a sibling of tmp_path (shares a prefix but is NOT under it).
    sibling = tmp_path.parent / (tmp_path.name + "_evil")
    sibling.mkdir(exist_ok=True)
    payload = json.dumps({"version": 1, "logins": [{
        "id": "evil", "url": "https://evil.com", "label": "Evil",
        "has_state": True,
        "_files": {f"../{sibling.name}/storageState.json": "pwned"},
    }]})
    m = _FakeManager({}, tmp_path)
    r = m.import_browser_logins(payload)
    assert r["ok"]
    assert not (sibling / "storageState.json").exists()


def test_import_rejects_oversized_file(tmp_path: Path, monkeypatch):
    """An oversized state blob must be skipped, not written."""
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    huge = "x" * (6 * 1024 * 1024)  # 6 MiB — over the 5 MiB per-file limit
    payload = json.dumps({"version": 1, "logins": [{
        "id": "big-com", "url": "https://big.com", "label": "Big",
        "has_state": True,
        "_files": {"browser_profiles/big-com/storageState.json": huge},
    }]})
    m = _FakeManager({}, tmp_path)
    r = m.import_browser_logins(payload)
    assert r["ok"]
    assert not (tmp_path / "browser_profiles" / "big-com" / "storageState.json").exists()


def test_import_cannot_read_secrets_via_files(tmp_path: Path, monkeypatch):
    """The _files keys are display-only metadata from the export; import must NOT read or
    overwrite arbitrary files like secrets.json. Only canonical filenames are written."""
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    # Pre-create a secrets file; the import must not touch it via _files.
    (tmp_path / "secrets.json").write_text('{"real_secret": "sk-xxx"}', encoding="utf-8")
    payload = json.dumps({"version": 1, "logins": [{
        "id": "evil-com", "url": "https://evil.com", "label": "Evil",
        "has_state": True,
        "_files": {"secrets.json": '{"stolen": true}'},
    }]})
    m = _FakeManager({}, tmp_path)
    r = m.import_browser_logins(payload)
    assert r["ok"]
    # secrets.json must still have the original content — not overwritten.
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))["real_secret"] == "sk-xxx"


def test_import_writes_canonical_filenames_only(tmp_path: Path, monkeypatch):
    """Only storageState.json and cookies.json are written under the profile dir; a non-
    canonical filename in _files is filtered out by basename, not written to disk."""
    monkeypatch.setattr("coworker.server.manager.state_dir", lambda: tmp_path)
    # Two separate logins: one with only canonical files (accepted), one sneaking in evil.exe
    # alongside them (the evil.exe must be filtered, but the canonical files still write).
    payload = json.dumps({"version": 1, "logins": [
        {
            "id": "ok-com", "url": "https://ok.com", "label": "OK",
            "has_state": True,
            "_files": {
                "browser_profiles/ok-com/storageState.json": '{"cookies": []}',
                "browser_profiles/ok-com/cookies.json": '[{"name": "s"}]',
            },
        },
        {
            "id": "sneak-com", "url": "https://sneak.com", "label": "Sneak",
            "has_state": True,
            "_files": {
                "browser_profiles/sneak-com/storageState.json": '{"cookies": []}',
                "browser_profiles/sneak-com/evil.exe": "MALWARE",
            },
        },
    ]})
    m = _FakeManager({}, tmp_path)
    r = m.import_browser_logins(payload)
    assert r["ok"] and r["imported"] == 2
    prof_ok = tmp_path / "browser_profiles" / "ok-com"
    assert (prof_ok / "storageState.json").is_file()
    assert (prof_ok / "cookies.json").is_file()
    prof_sneak = tmp_path / "browser_profiles" / "sneak-com"
    assert (prof_sneak / "storageState.json").is_file()
    assert not (prof_sneak / "evil.exe").exists()  # non-canonical file rejected


