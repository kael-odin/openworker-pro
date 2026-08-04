"""Plugin marketplace + install/uninstall (批次 E4).

Tests the plugin source manager, registry, marketplace.json parsing, and the four
``source`` kinds (git-subdir / url / github / local string) using local git fixtures
(no network). Mirrors ``tests/test_skill_sources.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coworker.plugins import (
    InstalledPlugin,
    PluginInstallError,
    PluginRegistry,
    PluginSource,
    PluginSourceManager,
    check_updates,
    install_plugin,
    list_catalog,
    list_installed,
    uninstall_plugin,
)


# -- PluginSourceManager (prefs persistence, builtin guard) ---------------------


def _prefs_and_mgr():
    prefs: dict = {}
    save = lambda: None  # noqa: E731 — in-memory; save is a no-op
    return prefs, PluginSourceManager(prefs, save)


def test_source_manager_ensure_builtins_seeds_official_source():
    prefs, mgr = _prefs_and_mgr()
    assert prefs.get("plugin_sources") is None
    mgr.ensure_builtins()
    sources = mgr.list()
    assert any(s.id == "claude-official" for s in sources)
    assert sources[0].is_default
    assert "claude-plugins-official" in sources[0].url


def test_source_manager_builtin_deletable_and_not_reasserted():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    # Builtins are now deletable (user chose "deleted means deleted, no auto-restore").
    assert mgr.remove("claude-official") is True
    assert mgr.get("claude-official") is None
    # ensure_builtins() must NOT re-assert a deleted builtin — the deletion is recorded
    # in the deleted_builtin_plugin_sources pref so it survives restarts.
    mgr.ensure_builtins()
    assert mgr.get("claude-official") is None
    # Disabling still works for sources that remain.
    mgr.add("Temp", "https://example.com/foo.git")
    # The deleted-builtin record is persisted in prefs.
    deleted = prefs.get("deleted_builtin_plugin_sources") or []
    assert "claude-official" in deleted


def test_source_manager_reset_restores_deleted_builtin():
    """reset() clears the deleted-builtin record and brings back all builtins."""
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    mgr.remove("claude-official")
    assert mgr.get("claude-official") is None
    sources = mgr.reset()
    assert any(s.id == "claude-official" for s in sources)
    # The deleted-builtin record is cleared so ensure_builtins() won't re-delete it.
    assert "claude-official" not in (prefs.get("deleted_builtin_plugin_sources") or [])
    # reset() is idempotent — calling again is a no-op (builtin already present).
    sources2 = mgr.reset()
    assert any(s.id == "claude-official" for s in sources2)


def test_source_manager_add_update_remove_user_source():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    src = mgr.add("Community", "https://github.com/foo/bar.git")
    assert src.id.startswith("src-")
    assert src.source_type == "git"
    updated = mgr.update(src.id, {"name": "Renamed"})
    assert updated.name == "Renamed"
    assert mgr.remove(src.id) is True
    assert mgr.get(src.id) is None


def test_source_manager_add_rejects_empty_name_or_url():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    with pytest.raises(ValueError):
        mgr.add("", "http://x")
    with pytest.raises(ValueError):
        mgr.add("x", "")


def test_source_manager_prunes_stale_former_builtin():
    """A source that was once a builtin (is_default=True) but is no longer in
    BUILTIN_SOURCES should be pruned on ensure_builtins(). This handles the
    modelscope-skills migration: it was mis-filed as a plugin source in a prior
    release, persisted to prefs, then removed from BUILTIN_SOURCES. User-added
    sources (is_default=False) are never pruned."""
    prefs, mgr = _prefs_and_mgr()
    # Simulate a stale former-builtin left over in prefs from a prior release.
    prefs["plugin_sources"] = [
        PluginSource(
            id="claude-official",
            name="Claude 官方插件市场",
            url="https://github.com/anthropics/claude-plugins-official.git",
            is_default=True,
            source_type="git",
        ).to_dict(),
        PluginSource(
            id="modelscope-skills",  # former builtin, no longer in BUILTIN_SOURCES
            name="魔搭社区技能中心",
            url="https://github.com/modelscope/modelscope-skills.git",
            is_default=True,
            source_type="git",
        ).to_dict(),
    ]
    mgr.ensure_builtins()
    ids = [s.id for s in mgr.list()]
    assert "claude-official" in ids
    assert "modelscope-skills" not in ids  # pruned — no longer a plugin builtin


def test_source_manager_preserves_user_sources_through_prune():
    """User-added sources (is_default=False, id starting 'src-') must survive the
    former-builtin prune — only is_default=True stale entries are removed."""
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    user_src = mgr.add("My Community", "https://github.com/foo/bar.git")
    assert user_src.is_default is False
    mgr.ensure_builtins()  # re-run should not prune user sources


# -- Codex 3-field plugin source (ref + sparse_path) --------------------------


def test_plugin_source_ref_and_sparse_path_roundtrip():
    """PluginSource with ref + sparse_path serializes/deserializes losslessly."""
    src = PluginSource(
        id="src-test", name="Test", url="https://github.com/org/repo.git",
        source_type="git", ref="main", sparse_path="plugins/codex",
    )
    d = src.to_dict()
    assert d["ref"] == "main"
    assert d["sparse_path"] == "plugins/codex"
    restored = PluginSource.from_dict(d)
    assert restored.ref == "main"
    assert restored.sparse_path == "plugins/codex"


def test_plugin_source_from_dict_backwards_compatible_no_ref():
    """Old prefs without ref/sparse_path deserialize to empty strings (not errors)."""
    old = {"id": "src-x", "name": "X", "url": "https://x.git", "source_type": "git"}
    src = PluginSource.from_dict(old)
    assert src.ref == ""
    assert src.sparse_path == ""


def test_plugin_source_add_with_ref_and_sparse_path():
    """add() persists ref + sparse_path from the Codex-style 3-field form."""
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    src = mgr.add(
        "Codex Marketplace", "https://github.com/openai/plugins.git",
        ref="main", sparse_path="plugins/codex",
    )
    assert src.ref == "main"
    assert src.sparse_path == "plugins/codex"
    # Verify it's persisted in the raw prefs.
    raw = prefs["plugin_sources"]
    entry = next(r for r in raw if r["id"] == src.id)
    assert entry["ref"] == "main"
    assert entry["sparse_path"] == "plugins/codex"


def test_plugin_source_update_ref_and_sparse_path():
    """update() can change ref and sparse_path."""
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    src = mgr.add("X", "https://github.com/x/y.git")
    updated = mgr.update(src.id, {"ref": "v1.2", "sparse_path": "src/plugins"})
    assert updated.ref == "v1.2"
    assert updated.sparse_path == "src/plugins"
    assert mgr.get(src.id) is not None


# -- PluginRegistry ------------------------------------------------------------


def _registry():
    prefs: dict = {}
    save = lambda: None  # noqa: E731
    return PluginRegistry(prefs, save)


def test_registry_add_get_remove():
    reg = _registry()
    entry = InstalledPlugin(
        name="demo", version="1.0.0", source_id="claude-official",
        components={"skills": ["a"], "commands": [], "mcps": ["srv"]},
        sha="abc123",
    )
    reg.add(entry)
    assert reg.has("demo")
    got = reg.get("demo")
    assert got.version == "1.0.0"
    assert got.components["mcps"] == ["srv"]
    # Re-add overwrites.
    reg.add(InstalledPlugin(name="demo", version="2.0.0"))
    assert reg.get("demo").version == "2.0.0"
    assert reg.remove("demo") is True
    assert reg.has("demo") is False


def test_registry_update():
    reg = _registry()
    reg.add(InstalledPlugin(name="demo", sha="old", version="1.0.0"))
    reg.update("demo", {"sha": "new", "version": "1.1.0"})
    got = reg.get("demo")
    assert got.sha == "new"
    assert got.version == "1.1.0"


# -- marketplace.json parsing + local-string install (no network) --------------


def _git_init(repo: Path) -> None:
    """Init a git repo + commit everything (so it has a HEAD sha)."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True)


def _seed_marketplace(repo: Path) -> dict:
    """Build a fake marketplace repo with .claude-plugin/marketplace.json + 2 local plugins.

    Both plugins use the local-string source form (``./plugins/<name>``) so no second
    git repo is needed. ``alpha`` ships a skill + a command; ``beta`` ships an MCP server.
    """
    # plugin alpha: skill + command
    alpha = repo / "plugins" / "alpha"
    (alpha / ".claude-plugin").mkdir(parents=True)
    (alpha / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "alpha", "version": "1.0.0", "description": "Alpha plugin",
    }), encoding="utf-8")
    (alpha / "skills" / "greet").mkdir(parents=True)
    (alpha / "skills" / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: greet a user\n---\nSay hi.\n", encoding="utf-8"
    )
    (alpha / "commands" / "hello").mkdir(parents=True)
    (alpha / "commands" / "hello" / "COMMAND.md").write_text(
        "---\nname: hello\ndescription: say hello\n---\nHello {input}!\n", encoding="utf-8"
    )
    # plugin beta: mcp server
    beta = repo / "plugins" / "beta"
    (beta / ".claude-plugin").mkdir(parents=True)
    (beta / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "beta", "version": "0.9.0", "description": "Beta plugin",
        "mcpServers": {"beta-srv": {"command": "npx", "args": ["beta@0.9.0"]}},
    }), encoding="utf-8")
    # marketplace.json
    mp = {
        "name": "test-marketplace",
        "plugins": [
            {
                "name": "alpha", "description": "Alpha plugin", "category": "productivity",
                "source": "./plugins/alpha",
            },
            {
                "name": "beta", "description": "Beta plugin", "category": "development",
                "source": "./plugins/beta",
            },
        ],
    }
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps(mp), encoding="utf-8")
    _git_init(repo)
    return mp


def test_list_catalog_parses_marketplace_plugins(tmp_path: Path):
    repo = tmp_path / "market"
    _seed_marketplace(repo)
    prefs: dict = {}
    src = PluginSource(id="mp", name="MP", url=str(repo), source_type="git")
    items = list_catalog(src, tmp_path / "cache")
    names = [i["name"] for i in items]
    assert names == ["alpha", "beta"]
    alpha = next(i for i in items if i["name"] == "alpha")
    assert alpha["category"] == "productivity"
    assert alpha["source_info"]["source"] == "local"
    assert alpha["source_info"]["path"] == "./plugins/alpha"


def test_install_local_source_plugin_lands_skills_and_commands(tmp_path: Path):
    repo = tmp_path / "market"
    _seed_marketplace(repo)
    src = PluginSource(id="mp", name="MP", url=str(repo), source_type="git")
    plugins_dir = tmp_path / "plugins"
    cache_root = tmp_path / "cache"
    reg = _registry()
    result = install_plugin(src, "alpha", plugins_dir=plugins_dir, cache_root=cache_root, mcp_register=None)
    assert result["ok"]
    assert (plugins_dir / "alpha" / ".claude-plugin" / "plugin.json").is_file()
    assert (plugins_dir / "alpha" / "skills" / "greet" / "SKILL.md").is_file()
    assert result["components"]["skills"] == ["greet"]
    assert result["components"]["commands"] == ["hello"]
    assert result["components"]["mcps"] == []


def test_install_registers_mcp_servers_via_callback(tmp_path: Path):
    repo = tmp_path / "market"
    _seed_marketplace(repo)
    src = PluginSource(id="mp", name="MP", url=str(repo), source_type="git")
    registered: list[tuple[str, dict]] = []
    install_plugin(
        src, "beta",
        plugins_dir=tmp_path / "plugins", cache_root=tmp_path / "cache",
        mcp_register=lambda n, c: registered.append((n, c)),
    )
    assert registered == [("beta-srv", {"command": "npx", "args": ["beta@0.9.0"]})]


def test_uninstall_removes_folder_and_registry_and_unregisters_mcp(tmp_path: Path):
    repo = tmp_path / "market"
    _seed_marketplace(repo)
    src = PluginSource(id="mp", name="MP", url=str(repo), source_type="git")
    plugins_dir = tmp_path / "plugins"
    cache_root = tmp_path / "cache"
    reg = _registry()
    # Install beta (registers an MCP server).
    result = install_plugin(src, "beta", plugins_dir=plugins_dir, cache_root=cache_root, mcp_register=None)
    import datetime as _dt
    reg.add(InstalledPlugin(
        name="beta", source_id="mp", components=result["components"],
        installed_at=_dt.datetime.now().isoformat(timespec="seconds"),
    ))
    unregistered: list[str] = []
    uninstall_plugin("beta", plugins_dir=plugins_dir, registry=reg, mcp_unregister=lambda n: unregistered.append(n))
    assert not (plugins_dir / "beta").exists()
    assert reg.has("beta") is False
    assert unregistered == ["beta-srv"]


def test_install_unknown_plugin_raises(tmp_path: Path):
    repo = tmp_path / "market"
    _seed_marketplace(repo)
    src = PluginSource(id="mp", name="MP", url=str(repo), source_type="git")
    with pytest.raises(PluginInstallError):
        install_plugin(src, "nonexistent", plugins_dir=tmp_path / "p", cache_root=tmp_path / "c", mcp_register=None)


# -- _force_rmtree + _is_incomplete_clone (Windows-safe helpers) ---------------

def test_force_rmtree_removes_readonly_files(tmp_path: Path):
    """_force_rmtree must clear the read-only bit on .git-style files before removing.

    Git stores pack files as read-only on Windows; a plain shutil.rmtree raises
    PermissionError [WinError 5] on them. The helper's onexc/onerror handler clears
    the bit and retries, so the whole tree comes down. This test creates a read-only
    file (simulating a .git pack file) and confirms _force_rmtree removes it.
    """
    import os
    import stat

    from coworker.plugins.installer import _force_rmtree

    d = tmp_path / "plugin-with-git"
    git_dir = d / ".git" / "objects" / "pack"
    git_dir.mkdir(parents=True)
    pack = git_dir / "pack-abc.idx"
    pack.write_bytes(b"pack data")
    # Make it read-only (mimicking git's pack file permissions on Windows).
    os.chmod(pack, stat.S_IREAD)
    # A plain rmtree would fail on the read-only file; _force_rmtree must succeed.
    _force_rmtree(d)
    assert not d.exists()


def test_is_incomplete_clone_detects_git_only_dir(tmp_path: Path):
    """_is_incomplete_clone returns True when a cache dir has .git but no working tree.

    A clone interrupted mid-way (network drop, schannel TLS error) leaves .git but no
    checked-out files. This state must be detected so _git_clone_or_pull wipes and
    re-clones instead of trying `git pull` on a never-checked-out repo (exit 128).
    """
    from coworker.plugins.installer import _is_incomplete_clone

    # Incomplete: only .git, no working tree files.
    incomplete = tmp_path / "incomplete"
    (incomplete / ".git").mkdir(parents=True)
    assert _is_incomplete_clone(incomplete) is True

    # Complete: .git + at least one working-tree file.
    complete = tmp_path / "complete"
    (complete / ".git").mkdir(parents=True)
    (complete / "README.md").write_text("hello")
    assert _is_incomplete_clone(complete) is False

    # Not a clone at all (no .git).
    no_git = tmp_path / "no-git"
    no_git.mkdir()
    (no_git / "file.txt").write_text("data")
    assert _is_incomplete_clone(no_git) is False


def test_is_incomplete_clone_ignores_clone_age_marker(tmp_path: Path):
    """The .ow_clone_ts marker doesn't count as a working-tree file."""
    from coworker.plugins.installer import _is_incomplete_clone

    d = tmp_path / "marked"
    (d / ".git").mkdir(parents=True)
    (d / ".ow_clone_ts").write_text("12345.0")
    assert _is_incomplete_clone(d) is True


def test_safe_name_rejects_traversal():
    from coworker.plugins.installer import _safe_name
    with pytest.raises(PluginInstallError):
        _safe_name("../etc")
    with pytest.raises(PluginInstallError):
        _safe_name("")
    assert _safe_name("good-name") == "good-name"


def test_list_installed_enriches_with_presence(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "present").mkdir()
    reg = _registry()
    reg.add(InstalledPlugin(name="present", version="1.0.0"))
    reg.add(InstalledPlugin(name="absent", version="2.0.0"))
    items = list_installed(plugins_dir, reg)
    by_name = {i["name"]: i for i in items}
    assert by_name["present"]["present"] is True
    assert by_name["absent"]["present"] is False


# -- check_updates -------------------------------------------------------------


def test_check_updates_detects_sha_mismatch(tmp_path: Path):
    repo = tmp_path / "market"
    _seed_marketplace(repo)
    src = PluginSource(id="mp", name="MP", url=str(repo), source_type="git")
    reg = _registry()
    # Record an install with an old sha; the marketplace has no sha for local sources,
    # so we add a git-subdir entry to the marketplace to test sha comparison.
    mp = json.loads((repo / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    mp["plugins"].append({
        "name": "gamma", "description": "Gamma", "category": "dev",
        "source": {"source": "git-subdir", "url": str(repo), "path": "plugins/alpha", "sha": "newsha"},
    })
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps(mp), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "add gamma"], cwd=repo, check=True, capture_output=True)
    reg.add(InstalledPlugin(name="gamma", source_id="mp", sha="oldsha"))
    updates = check_updates(src, reg, tmp_path / "cache2")
    gamma = next(u for u in updates if u["name"] == "gamma")
    assert gamma["installed_sha"] == "oldsha"
    assert gamma["latest_sha"] == "newsha"
    assert gamma["up_to_date"] is False


# -- marketplace source-kind parsing (unit, no git) ----------------------------


def test_parse_plugin_entry_handles_four_source_kinds():
    from coworker.plugins.installer import _parse_plugin_entry

    # git-subdir
    e = _parse_plugin_entry({"name": "a", "source": {"source": "git-subdir", "url": "u", "path": "p", "sha": "s"}})
    assert e["source_info"]["source"] == "git-subdir"
    assert e["sha"] == "s"
    # url
    e = _parse_plugin_entry({"name": "b", "source": {"source": "url", "url": "u", "sha": "s"}})
    assert e["source_info"]["source"] == "url"
    # github
    e = _parse_plugin_entry({"name": "c", "source": {"source": "github", "repo": "o/r", "commit": "abc"}})
    assert e["source_info"]["repo"] == "o/r"
    # local string
    e = _parse_plugin_entry({"name": "d", "source": "./plugins/d"})
    assert e["source_info"]["source"] == "local"
    assert e["source_info"]["path"] == "./plugins/d"
    # author object
    e = _parse_plugin_entry({"name": "e", "author": {"name": "Foo"}, "source": "./p"})
    assert e["author"] == "Foo"
