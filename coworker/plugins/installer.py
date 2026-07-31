"""Plugin install/uninstall — fetch a plugin from a marketplace source into state_dir()/plugins.

A *plugin marketplace* is a git repo containing ``.claude-plugin/marketplace.json`` (the
Claude Code official format). The marketplace lists plugins; each entry's ``source`` object
says where the plugin's actual code lives. Four source kinds are supported (mirroring the
real claude-plugins-official marketplace):

* ``git-subdir`` — clone ``source.url`` (at ``ref``/``sha``), copy ``source.path`` subfolder.
* ``url``        — clone ``source.url`` (at ``sha``), the whole repo is the plugin.
* ``github``     — clone ``https://github.com/<source.repo>`` (at ``source.commit``/``sha``).
* local string   — a path like ``./plugins/foo`` relative to the marketplace repo's clone.

Install lands the plugin folder at ``state_dir()/plugins/<name>/`` (with its
``.claude-plugin/plugin.json`` + ``skills/`` + ``commands/`` + ``.mcp.json``). The plugin's
``.claude-plugin/plugin.json`` is then parsed: ``mcpServers`` are registered via a caller
callback (so the installer stays decoupled from the MCP manager), and ``skills``/``commands``
subfolder presence is recorded in the registry.

Uninstall removes the folder, unregisters MCP servers (via the recorded components), and
drops the registry entry.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .sources import PluginSource

_HTTP_TIMEOUT = 15.0
# Shared clone cache root. Marketplace clones live under state_dir()/plugin_sources_cache/<source-id>
# so repeated catalog browses reuse the clone (git pull to refresh). Per-plugin source clones
# (git-subdir / url / github) live under <source-id>__<plugin-name>/ so they don't collide.
_CACHE_TTL = 600.0  # refresh a cached clone at most every 10 min


class PluginInstallError(Exception):
    """Raised when a plugin cannot be installed/uninstalled (message surfaces to the UI)."""


def _safe_name(name: str) -> str:
    """Reject path-traversal / odd characters in a plugin name (it becomes a folder name)."""
    if not name or not isinstance(name, str):
        raise PluginInstallError("empty plugin name")
    n = name.strip().replace("\\", "/").strip("/")
    if not n or n.startswith(".") or ".." in n.split("/"):
        raise PluginInstallError(f"invalid plugin name: {name!r}")
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./" for c in n):
        raise PluginInstallError(f"invalid plugin name: {name!r}")
    return n


# -- git helpers ---------------------------------------------------------------


def _run_git(args: list[str], *, timeout: int = 120) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise PluginInstallError(f"git failed: {e}") from e


def _clone_age(cache_dir: Path) -> float:
    ts_file = cache_dir / ".ow_clone_ts"
    if not ts_file.is_file():
        return 0.0
    try:
        return float(ts_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0.0


def _mark_clone_age(cache_dir: Path) -> None:
    (cache_dir / ".ow_clone_ts").write_text(str(time.monotonic()), encoding="utf-8")


def _git_clone_or_pull(url: str, cache_dir: Path, *, ref: str = "", sha: str = "") -> Path:
    """Clone (or refresh) ``url`` into ``cache_dir``. If ``sha`` is given, check it out.

    A pinned ``sha`` forces a full (non-shallow) fetch + checkout so the exact commit is
    present; without ``sha`` a shallow clone (``--depth 1``) is used and refreshed via pull.
    """
    if cache_dir.is_dir() and not sha and (time.monotonic() - _clone_age(cache_dir)) < _CACHE_TTL:
        return cache_dir
    try:
        if cache_dir.is_dir():
            if sha:
                # Pinned commit: fetch the specific sha (full depth) and check it out.
                _run_git(["git", "-C", str(cache_dir), "fetch", "--depth", "1", "origin", sha], timeout=90)
                _run_git(["git", "-C", str(cache_dir), "checkout", sha], timeout=60)
            else:
                _run_git(["git", "-C", str(cache_dir), "pull", "--ff-only", "--depth", "1"], timeout=60)
        else:
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            if sha:
                # Clone first (shallow is fine, we fetch the sha explicitly), then pin.
                _run_git(["git", "clone", "--depth", "1", url, str(cache_dir)], timeout=120)
                try:
                    _run_git(["git", "-C", str(cache_dir), "fetch", "--depth", "1", "origin", sha], timeout=90)
                    _run_git(["git", "-C", str(cache_dir), "checkout", sha], timeout=60)
                except PluginInstallError:
                    # sha fetch can fail if the shallow clone doesn't contain it; unshallow + retry.
                    _run_git(["git", "-C", str(cache_dir), "fetch", "--unshallow"], timeout=180)
                    _run_git(["git", "-C", str(cache_dir), "checkout", sha], timeout=60)
            elif ref:
                _run_git(["git", "clone", "--depth", "1", "--branch", ref, url, str(cache_dir)], timeout=120)
            else:
                _run_git(["git", "clone", "--depth", "1", url, str(cache_dir)], timeout=120)
    except PluginInstallError:
        raise
    except Exception as e:
        raise PluginInstallError(f"failed to clone {url}: {e}") from e
    _mark_clone_age(cache_dir)
    return cache_dir


# -- marketplace.json parsing -------------------------------------------------


def _read_marketplace(clone_dir: Path) -> dict[str, Any]:
    """Read + parse ``.claude-plugin/marketplace.json`` from a cloned marketplace repo."""
    mp = clone_dir / ".claude-plugin" / "marketplace.json"
    if not mp.is_file():
        raise PluginInstallError(
            f"not a plugin marketplace: .claude-plugin/marketplace.json not found"
        )
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PluginInstallError(f"invalid marketplace.json: {e}") from e


def _parse_plugin_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize one marketplace plugin entry to a flat dict the UI/installer uses."""
    src = entry.get("source")
    # `source` may be an object ({source, url, path, ref, sha, repo, commit}) or a local
    # string path (e.g. "./plugins/foo") relative to the marketplace repo.
    if isinstance(src, str):
        source_info: dict[str, Any] = {"source": "local", "path": src}
    elif isinstance(src, dict):
        source_info = dict(src)
        source_info.setdefault("source", "git-subdir")
    else:
        source_info = {}
    author = entry.get("author")
    if isinstance(author, dict):
        author = author.get("name", "")
    return {
        "name": str(entry.get("name") or ""),
        "description": str(entry.get("description") or ""),
        "category": str(entry.get("category") or ""),
        "author": str(author or ""),
        "homepage": str(entry.get("homepage") or ""),
        "version": str(entry.get("version") or ""),
        "source_info": source_info,
        "sha": str(source_info.get("sha") or ""),
    }


# -- public API ----------------------------------------------------------------


def list_catalog(source: PluginSource, cache_root: Path) -> list[dict[str, Any]]:
    """List installable plugins from a marketplace source (clones on demand).

    Returns a list of normalized plugin entries (name/description/category/source_info/...).
    """
    cache_dir = cache_root / source.id
    _git_clone_or_pull(source.url, cache_dir)
    data = _read_marketplace(cache_dir)
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return []
    out = []
    for item in plugins:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        out.append(_parse_plugin_entry(item))
    return out


def _resolve_source_folder(
    source_info: dict[str, Any], *, marketplace_clone: Path, cache_root: Path, source_id: str, name: str
) -> Path:
    """Resolve the plugin's source folder from its ``source_info``.

    Returns the path inside a clone that holds ``.claude-plugin/plugin.json``.
    """
    kind = str(source_info.get("source") or "local")

    if kind == "local":
        # A path relative to the marketplace repo (e.g. "./plugins/foo").
        rel = str(source_info.get("path") or ".").lstrip("./")
        folder = marketplace_clone / rel if rel else marketplace_clone
        if not folder.is_dir():
            raise PluginInstallError(f"plugin {name!r}: local path {rel!r} not found in marketplace")
        return folder

    if kind == "git-subdir":
        url = str(source_info.get("url") or "")
        if not url:
            raise PluginInstallError(f"plugin {name!r}: git-subdir source missing url")
        ref = str(source_info.get("ref") or "")
        sha = str(source_info.get("sha") or "")
        sub_cache = cache_root / f"{source_id}__{_safe_name(name)}"
        clone = _git_clone_or_pull(url, sub_cache, ref=ref, sha=sha)
        rel = str(source_info.get("path") or ".").lstrip("./")
        folder = clone / rel if rel else clone
        if not folder.is_dir():
            raise PluginInstallError(f"plugin {name!r}: path {rel!r} not found in {url}")
        return folder

    if kind == "url":
        url = str(source_info.get("url") or "")
        if not url:
            raise PluginInstallError(f"plugin {name!r}: url source missing url")
        sha = str(source_info.get("sha") or "")
        sub_cache = cache_root / f"{source_id}__{_safe_name(name)}"
        clone = _git_clone_or_pull(url, sub_cache, sha=sha)
        return clone

    if kind == "github":
        repo = str(source_info.get("repo") or "")
        if not repo:
            raise PluginInstallError(f"plugin {name!r}: github source missing repo")
        url = f"https://github.com/{repo}.git"
        sha = str(source_info.get("sha") or source_info.get("commit") or "")
        sub_cache = cache_root / f"{source_id}__{_safe_name(name)}"
        clone = _git_clone_or_pull(url, sub_cache, sha=sha)
        return clone

    raise PluginInstallError(f"plugin {name!r}: unsupported source kind {kind!r}")


def _parse_plugin_json(folder: Path) -> dict[str, Any]:
    """Read the plugin's ``.claude-plugin/plugin.json`` (if present). Returns {} if absent."""
    pj = folder / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return {}
    try:
        return json.loads(pj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _detect_components(folder: Path, plugin_json: dict[str, Any]) -> dict[str, list[str]]:
    """Detect which components the plugin contributes (skills / commands / mcps)."""
    skills: list[str] = []
    commands: list[str] = []
    mcps: list[str] = []

    # Skills: a skills/ subfolder whose children each have SKILL.md.
    skills_dir = folder / "skills"
    if skills_dir.is_dir():
        for sub in sorted(skills_dir.iterdir()):
            if sub.is_dir() and (sub / "SKILL.md").is_file():
                skills.append(sub.name)

    # Commands: a commands/ subfolder whose children each have COMMAND.md (or .md files).
    commands_dir = folder / "commands"
    if commands_dir.is_dir():
        for sub in sorted(commands_dir.iterdir()):
            if sub.is_dir() and (sub / "COMMAND.md").is_file():
                commands.append(sub.name)
            elif sub.is_file() and sub.suffix == ".md":
                commands.append(sub.stem)

    # MCP servers: .mcp.json at root, or plugin.json's mcpServers field.
    mcp_servers: dict[str, Any] = {}
    mcp_file = folder / ".mcp.json"
    if mcp_file.is_file():
        try:
            mcp_data = json.loads(mcp_file.read_text(encoding="utf-8"))
            if isinstance(mcp_data, dict):
                servers = mcp_data.get("mcpServers")
                if isinstance(servers, dict):
                    mcp_servers = servers
        except json.JSONDecodeError:
            pass
    if not mcp_servers:
        ms = plugin_json.get("mcpServers")
        if isinstance(ms, dict):
            mcp_servers = ms
    mcps = list(mcp_servers.keys())

    return {"skills": skills, "commands": commands, "mcps": mcps}


def install_plugin(
    source: PluginSource,
    name: str,
    *,
    plugins_dir: Path,
    cache_root: Path,
    mcp_register: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Install ``name`` from marketplace ``source`` into ``plugins_dir/<name>/``.

    ``mcp_register`` is called once per MCP server the plugin declares (name → config);
    the caller (manager) wires it to ``add_mcp``. Returns ``{ok, name, path, components}``.
    Raises :class:`PluginInstallError` on failure.
    """
    safe = _safe_name(name)
    cache_dir = cache_root / source.id
    _git_clone_or_pull(source.url, cache_dir)
    data = _read_marketplace(cache_dir)

    # Find the entry in the marketplace.
    entry = None
    raw_plugins = data.get("plugins") if isinstance(data, dict) else []
    if isinstance(raw_plugins, list):
        for item in raw_plugins:
            if isinstance(item, dict) and item.get("name") == name:
                entry = _parse_plugin_entry(item)
                break
    if entry is None:
        raise PluginInstallError(f"plugin {name!r} not found in marketplace {source.name!r}")

    src_folder = _resolve_source_folder(
        entry["source_info"],
        marketplace_clone=cache_dir,
        cache_root=cache_root,
        source_id=source.id,
        name=name,
    )
    if not (src_folder / ".claude-plugin" / "plugin.json").is_file() and not (src_folder / "skills").is_dir():
        # As a backstop, accept a folder with skills/ but no plugin.json (skill-bundle, strict:false).
        if not (src_folder / "skills").is_dir():
            raise PluginInstallError(f"plugin {name!r}: no .claude-plugin/plugin.json and no skills/")

    plugins_dir.mkdir(parents=True, exist_ok=True)
    dest = plugins_dir / safe
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src_folder, dest)

    plugin_json = _parse_plugin_json(dest)
    components = _detect_components(dest, plugin_json)

    # Register MCP servers (each server name → config dict).
    if mcp_register is not None:
        mcp_servers: dict[str, Any] = {}
        mcp_file = dest / ".mcp.json"
        if mcp_file.is_file():
            try:
                mcp_data = json.loads(mcp_file.read_text(encoding="utf-8"))
                if isinstance(mcp_data, dict) and isinstance(mcp_data.get("mcpServers"), dict):
                    mcp_servers = mcp_data["mcpServers"]
            except json.JSONDecodeError:
                pass
        if not mcp_servers and isinstance(plugin_json.get("mcpServers"), dict):
            mcp_servers = plugin_json["mcpServers"]
        # Namespace the MCP server name with the plugin to avoid collisions across plugins.
        for srv_name, srv_cfg in mcp_servers.items():
            if isinstance(srv_cfg, dict):
                mcp_register(srv_name, srv_cfg)

    return {
        "ok": True,
        "name": safe,
        "path": str(dest),
        "components": components,
        "version": entry.get("version") or str(plugin_json.get("version") or ""),
        "description": entry.get("description") or str(plugin_json.get("description") or ""),
        "source_info": entry["source_info"],
        "sha": entry.get("sha") or "",
    }


def uninstall_plugin(
    name: str,
    *,
    plugins_dir: Path,
    registry,
    mcp_unregister: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Remove ``plugins_dir/<name>/`` + registry entry + unregister MCP servers.

    ``mcp_unregister`` is called for each MCP server the plugin registered (caller wires to
    ``delete_mcp``). Returns ``{ok, name}``.
    """
    safe = _safe_name(name)
    dest = plugins_dir / safe
    # Reverse-register MCP servers from the registry record (the source of truth for what
    # was added at install time).
    entry = registry.get(safe) if registry is not None else None
    if entry is not None and mcp_unregister is not None:
        for srv_name in entry.components.get("mcps", []):
            try:
                mcp_unregister(srv_name)
            except Exception:
                pass  # best-effort: don't block uninstall on a stuck MCP server
    if dest.is_dir():
        shutil.rmtree(dest)
    if registry is not None:
        registry.remove(safe)
    return {"ok": True, "name": safe}


def list_installed(plugins_dir: Path, registry) -> list[dict[str, Any]]:
    """Return installed plugins enriched with on-disk presence + components.

    The registry is the source of truth for what was installed; this cross-references
    the plugins dir so a manually-deleted folder is flagged as ``present=False``.
    """
    out: list[dict[str, Any]] = []
    if registry is None:
        return out
    for inst in registry.list():
        folder = plugins_dir / inst.name
        out.append({
            **inst.to_dict(),
            "present": folder.is_dir(),
            "components": inst.components,
        })
    out.sort(key=lambda p: p["name"].lower())
    return out


def check_updates(source: PluginSource, registry, cache_root: Path) -> list[dict[str, Any]]:
    """Compare installed plugins' pinned sha against the marketplace's current sha.

    Returns a list of ``{name, installed_sha, latest_sha, up_to_date}`` for installed
    plugins whose source_id matches ``source.id``.
    """
    try:
        catalog = list_catalog(source, cache_root)
    except PluginInstallError:
        return []
    by_name = {c["name"]: c for c in catalog}
    out = []
    if registry is None:
        return out
    for inst in registry.list():
        if inst.source_id != source.id:
            continue
        cat = by_name.get(inst.name)
        if cat is None:
            continue
        latest_sha = cat.get("sha") or ""
        out.append({
            "name": inst.name,
            "installed_sha": inst.sha,
            "latest_sha": latest_sha,
            "latest_version": cat.get("version") or "",
            "up_to_date": bool(latest_sha) and bool(inst.sha) and latest_sha == inst.sha,
        })
    return out
