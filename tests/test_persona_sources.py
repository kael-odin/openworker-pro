"""Persona marketplace source management + browse/install (批次 E4 后续).

Tests the PersonaSourceManager (prefs persistence, builtin guard — though there
are no builtins today), the marketplace browse (list_catalog scans a fake-cloned
repo for *.md manifests), and install (reuses PersonaRegistry.install_from_dir so
the consent + disabled-pending-approval lifecycle is preserved). Git clone is
faked by monkeypatching _git_clone_or_pull so no network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coworker.personas import (
    PersonaMarketplaceError,
    PersonaSource,
    PersonaSourceManager,
    install_persona,
    list_catalog,
)
from coworker.personas import marketplace as mp
from coworker.personas.registry import PersonaRegistry


# -- PersonaSourceManager (prefs persistence, CRUD) ---------------------------


def _prefs_and_mgr():
    prefs: dict = {}
    save = lambda: None  # noqa: E731 — in-memory; save is a no-op
    return prefs, PersonaSourceManager(prefs, save)


def test_source_manager_empty_by_default():
    _, mgr = _prefs_and_mgr()
    # No builtins today (BUILTIN_SOURCES is empty) — list is empty until the user adds one.
    assert mgr.list() == []


def test_source_manager_ensure_builtins_noop_when_empty():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    # No builtin sources today, so ensure_builtins seeds nothing.
    assert prefs.get("persona_sources") is None or prefs["persona_sources"] == []


def test_source_manager_add_update_remove_user_source():
    _, mgr = _prefs_and_mgr()
    src = mgr.add("My personas", "https://github.com/foo/bar.git")
    assert src.id.startswith("src-")
    assert src.url == "https://github.com/foo/bar.git"
    assert src.is_default is False
    # Update name.
    updated = mgr.update(src.id, {"name": "Renamed"})
    assert updated.name == "Renamed"
    # Disable + re-enable.
    mgr.update(src.id, {"enabled": False})
    assert mgr.list(enabled_only=True) == []
    mgr.update(src.id, {"enabled": True})
    assert len(mgr.list(enabled_only=True)) == 1
    # User source is deletable (no builtins to protect).
    assert mgr.remove(src.id) is True
    assert mgr.get(src.id) is None


def test_source_manager_add_rejects_empty_name_or_url():
    _, mgr = _prefs_and_mgr()
    with pytest.raises(ValueError):
        mgr.add("", "http://x")
    with pytest.raises(ValueError):
        mgr.add("x", "")


def test_source_manager_remove_nonexistent_returns_false():
    _, mgr = _prefs_and_mgr()
    assert mgr.remove("does-not-exist") is False


# -- marketplace browse + install (fake-cloned repo) --------------------------


def _seed_persona_repo(repo: Path) -> dict:
    """Build a fake persona marketplace repo: root + subfolder *.md manifests.

    Includes a README.md (not a persona — must be skipped) and a malformed md
    (no frontmatter — must be skipped) to exercise the catalog filter.
    """
    # A valid persona manifest at the root.
    (repo / "ops.md").write_text(
        "---\n"
        "id: ops\n"
        "name: Ops\n"
        "tagline: run operations\n"
        "description: operational helper\n"
        "family: knowledge\n"
        "---\n"
        "You are an ops persona.",
        encoding="utf-8",
    )
    # A valid persona in a subfolder (nested manifest is discovered too).
    sub = repo / "team" / "researcher"
    sub.mkdir(parents=True)
    (sub / "manifest.md").write_text(
        "---\n"
        "id: researcher\n"
        "name: Researcher\n"
        "tagline: deep research\n"
        "family: knowledge\n"
        "---\n"
        "You do deep research.",
        encoding="utf-8",
    )
    # A code-family persona.
    (repo / "coder.md").write_text(
        "---\n"
        "id: coder\n"
        "name: Coder\n"
        "family: code\n"
        "---\n"
        "You write code.",
        encoding="utf-8",
    )
    # README.md — not a persona (no frontmatter) → skipped.
    (repo / "README.md").write_text("# My persona repo\n\nInstall these.", encoding="utf-8")
    # A malformed manifest — no closing frontmatter → skipped.
    (repo / "broken.md").write_text("---\nid: broken\nname: Broken\n", encoding="utf-8")
    return {"ops", "researcher", "coder"}


@pytest.fixture
def fake_clone(monkeypatch, tmp_path):
    """Patch _git_clone_or_pull to copy a seeded repo into the cache dir instead
    of cloning from the network. Returns the seeded repo path for assertions."""
    repo_src = tmp_path / "repo_src"
    repo_src.mkdir()
    _seed_persona_repo(repo_src)

    def _fake(url, cache_dir):
        # Simulate a clone: copy the seeded repo tree into cache_dir.
        if not cache_dir.is_dir():
            import shutil

            shutil.copytree(repo_src, cache_dir)
        return cache_dir

    monkeypatch.setattr(mp, "_git_clone_or_pull", _fake)
    return repo_src


def test_list_catalog_discovers_all_personal(fake_clone, tmp_path):
    src = PersonaSource(id="s1", name="test", url="https://example.git")
    catalog = list_catalog(src, tmp_path / "cache")
    ids = {c["id"] for c in catalog}
    assert ids == {"ops", "researcher", "coder"}
    # Each entry carries the catalog-display fields.
    for c in catalog:
        assert c["name"] and c["file"] and c["family"] in {"code", "knowledge"}
    # README.md and broken.md were skipped (not in the catalog).
    files = {c["file"] for c in catalog}
    assert "README.md" not in files
    assert "broken.md" not in files
    # Nested manifest discovered with its repo-relative path.
    assert any(c["id"] == "researcher" and c["file"].endswith("manifest.md") for c in catalog)


def test_list_catalog_carry_tagline_and_description(fake_clone, tmp_path):
    src = PersonaSource(id="s1", name="test", url="https://example.git")
    catalog = list_catalog(src, tmp_path / "cache")
    by_id = {c["id"]: c for c in catalog}
    assert by_id["ops"]["tagline"] == "run operations"
    assert by_id["ops"]["description"] == "operational helper"


def test_install_persona_from_marketplace_lands_disabled_pending_consent(
    fake_clone, tmp_path
):
    """Installing from a marketplace reuses install_from_dir: the persona is
    snapshotted into the managed area, lands disabled + unsurfaced, and the
    registry reports has_state correctly. No consent auto-granted."""
    registry = PersonaRegistry(
        state_path=tmp_path / "personas.json",
        installed_dir=tmp_path / "personas-installed",
    )
    src = PersonaSource(id="s1", name="test", url="https://example.git")
    result = install_persona(src, "ops", registry=registry, cache_root=tmp_path / "cache")
    assert result["ok"] is True
    assert result["consent"]["id"] == "ops"
    # The persona is now in the registry, disabled pending consent.
    entry = registry.get("ops")
    assert entry is not None
    assert entry.builtin is False
    assert registry.is_enabled("ops") is False
    assert registry.is_surfaced("ops") is False
    # The snapshot landed on disk.
    assert (tmp_path / "personas-installed" / "ops" / "manifest.md").is_file()


def test_install_persona_from_subfolder_manifest(fake_clone, tmp_path):
    """A nested manifest (team/researcher/manifest.md) installs by id, not path."""
    registry = PersonaRegistry(
        state_path=tmp_path / "personas.json",
        installed_dir=tmp_path / "personas-installed",
    )
    src = PersonaSource(id="s1", name="test", url="https://example.git")
    result = install_persona(src, "researcher", registry=registry, cache_root=tmp_path / "cache")
    assert result["ok"] is True
    assert result["consent"]["id"] == "researcher"
    assert registry.get("researcher") is not None


def test_install_persona_not_in_catalog_raises(fake_clone, tmp_path):
    registry = PersonaRegistry(
        state_path=tmp_path / "personas.json",
        installed_dir=tmp_path / "personas-installed",
    )
    src = PersonaSource(id="s1", name="test", url="https://example.git")
    with pytest.raises(PersonaMarketplaceError):
        install_persona(src, "nonexistent", registry=registry, cache_root=tmp_path / "cache")


def test_install_only_installs_the_requested_persona_not_all(fake_clone, tmp_path):
    """install_persona copies exactly one manifest into a temp dir before calling
    install_from_dir — so sibling manifests in the repo are NOT installed."""
    registry = PersonaRegistry(
        state_path=tmp_path / "personas.json",
        installed_dir=tmp_path / "personas-installed",
    )
    src = PersonaSource(id="s1", name="test", url="https://example.git")
    install_persona(src, "ops", registry=registry, cache_root=tmp_path / "cache")
    # Only ops landed; researcher and coder did not.
    assert registry.get("ops") is not None
    assert registry.get("researcher") is None
    assert registry.get("coder") is None


def test_uninstall_persona_from_marketplace(fake_clone, tmp_path):
    registry = PersonaRegistry(
        state_path=tmp_path / "personas.json",
        installed_dir=tmp_path / "personas-installed",
    )
    src = PersonaSource(id="s1", name="test", url="https://example.git")
    install_persona(src, "ops", registry=registry, cache_root=tmp_path / "cache")
    # User approves (enable) then later uninstalls.
    registry.set_enabled("ops", True)
    from coworker.personas import uninstall_persona as _uninstall

    result = _uninstall("ops", registry=registry)
    assert result["ok"] is True
    assert registry.get("ops") is None
    # Snapshot dir cleared.
    assert not (tmp_path / "personas-installed" / "ops").exists()
