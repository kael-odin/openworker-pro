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


def test_source_manager_builtin_cannot_be_deleted_only_disabled():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    assert mgr.remove("claude-official") is False
    mgr.update("claude-official", {"enabled": False})
    assert mgr.get("claude-official").enabled is False
    assert mgr.list(enabled_only=True) == []


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
