"""Skill install/uninstall — fetch a SKILL.md folder from a source into state_dir()/skills.

Three source kinds, one outcome (a ``<name>/SKILL.md`` folder under ``state_dir()/skills``):

* ``git``  — clone (or update) a GitHub repo into a shared cache, then copy the named subfolder.
  This serves the Anthropic official skills repo, which is a plain directory of SKILL.md folders
  with no index.json. The clone is shallow and shared across installs from the same source.
* ``local`` — copy a subfolder from a local directory (the existing SkillLoader discovery layout).
* ``http`` — fetch ``index.json`` then ``SKILL.md`` (+ any ``files`` the entry lists) from an HTTP
  base URL. Mirrors DHP's DhpHttpAdapter for sources that publish a structured catalog.

Install is idempotent: re-installing overwrites. Uninstall removes the folder (builtins marked
in the SKILL.md frontmatter's ``x-openworker-builtin`` are protected — but in practice builtin
skills ship with the app, not via this path, so the guard is a backstop).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .sources import SkillSource

_HTTP_TIMEOUT = 15.0
# Shared clone cache root. Clones live under state_dir()/skill_sources_cache/<source-id> so
# repeated installs from the same git source reuse the clone (git pull to refresh).
_CACHE_TTL = 600.0  # refresh a cached clone at most every 10 min


class SkillInstallError(Exception):
    """Raised when a skill cannot be installed/uninstalled (message surfaces to the UI)."""


def _safe_name(name: str) -> str:
    """Reject path-traversal / odd characters in a skill name (it becomes a folder name)."""
    if not name or not isinstance(name, str):
        raise SkillInstallError("empty skill name")
    n = name.strip().replace("\\", "/").strip("/")
    if not n or n.startswith(".") or ".." in n.split("/"):
        raise SkillInstallError(f"invalid skill name: {name!r}")
    # Allow only filesystem-safe chars (letters, digits, dash, underscore, dot, slash for nesting).
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./" for c in n):
        raise SkillInstallError(f"invalid skill name: {name!r}")
    return n


# -- git source ----------------------------------------------------------------


def _git_clone_or_pull(source: SkillSource, cache_root: Path) -> Path:
    """Return a fresh-enough clone of the source repo under ``cache_root``.

    Clones shallowly on first use; on subsequent calls, refreshes via ``git pull`` only if the
    cache is older than _CACHE_TTL (so browsing the catalog doesn't hammer GitHub).
    """
    cache_dir = cache_root / source.id
    if cache_dir.is_dir() and (time.monotonic() - _clone_age(cache_dir)) < _CACHE_TTL:
        return cache_dir
    try:
        if cache_dir.is_dir():
            # Refresh in place. --ff-only avoids surprise merge commits on a cache we own.
            subprocess.run(
                ["git", "-C", str(cache_dir), "pull", "--ff-only", "--depth", "1"],
                check=True, capture_output=True, timeout=60,
            )
        else:
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", source.url, str(cache_dir)],
                check=True, capture_output=True, timeout=120,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        # FileNotFoundError = git not installed; CalledProcessError = clone/pull failed.
        raise SkillInstallError(
            f"failed to clone skill source {source.name!r}: {e}"
        ) from e
    (cache_dir / ".ow_clone_ts").write_text(str(time.monotonic()), encoding="utf-8")
    return cache_dir


def _clone_age(cache_dir: Path) -> float:
    ts_file = cache_dir / ".ow_clone_ts"
    if not ts_file.is_file():
        return 0.0
    try:
        return float(ts_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0.0


def _list_git_skills(clone_dir: Path) -> list[dict[str, Any]]:
    """Discover installable skills in a cloned repo: any subfolder with a SKILL.md.

    Reads the SKILL.md frontmatter for name/description so the catalog matches what install
    will actually lay down. Top-level ``SKILL.md`` (a single-skill repo) is also supported.
    """
    from .base import _parse_skill  # reuse the existing frontmatter parser

    out: list[dict[str, Any]] = []
    # Single-skill repo: SKILL.md at root.
    root_md = clone_dir / "SKILL.md"
    if root_md.is_file():
        try:
            sk = _parse_skill(root_md)
            out.append({"name": sk.name, "description": sk.description, "path": ""})
        except Exception:
            pass
    # Multi-skill repo: each immediate subfolder with a SKILL.md.
    for sub in sorted(clone_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        md = sub / "SKILL.md"
        if md.is_file():
            try:
                sk = _parse_skill(md)
                out.append({"name": sk.name or sub.name, "description": sk.description, "path": sub.name})
            except Exception:
                out.append({"name": sub.name, "description": "", "path": sub.name})
    return out


# -- http source ---------------------------------------------------------------


def _http_get(url: str) -> str:
    try:
        resp = httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True).get(url)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        raise SkillInstallError(f"failed to fetch {url}: {e}") from e


def _list_http_skills(source: SkillSource) -> list[dict[str, Any]]:
    """Fetch ``index.json`` from an HTTP skill source."""
    import json

    base = source.url.rstrip("/")
    text = _http_get(f"{base}/index.json")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SkillInstallError(f"invalid index.json from {source.name!r}: {e}") from e
    skills = []
    for item in (data.get("skills") or []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("name"):
            skills.append({
                "name": str(item["name"]),
                "description": str(item.get("description", "")),
                "path": str(item.get("path") or item["name"]),
            })
    return skills


# -- local source --------------------------------------------------------------


def _list_local_skills(source: SkillSource) -> list[dict[str, Any]]:
    """Discover skills in a local directory (subfolders with SKILL.md)."""
    from .base import _parse_skill

    base = Path(source.url)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for sub in sorted(base.iterdir()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        md = sub / "SKILL.md"
        if md.is_file():
            try:
                sk = _parse_skill(md)
                out.append({"name": sk.name or sub.name, "description": sk.description, "path": sub.name})
            except Exception:
                out.append({"name": sub.name, "description": "", "path": sub.name})
    return out


# -- public API ----------------------------------------------------------------


def list_catalog(source: SkillSource, cache_root: Path) -> list[dict[str, Any]]:
    """List installable skills from a source (name + description + path)."""
    if source.source_type == "git":
        clone_dir = _git_clone_or_pull(source, cache_root)
        return _list_git_skills(clone_dir)
    if source.source_type == "local":
        return _list_local_skills(source)
    # default: http
    return _list_http_skills(source)


def install_skill(
    source: SkillSource,
    name: str,
    *,
    skills_dir: Path,
    cache_root: Path,
) -> dict[str, Any]:
    """Install ``name`` from ``source`` into ``skills_dir/<name>/``.

    Returns ``{"ok": True, "name": ..., "path": ...}``. Raises :class:`SkillInstallError` on
    failure (missing skill, fetch error, git unavailable).
    """
    safe = _safe_name(name)
    skills_dir.mkdir(parents=True, exist_ok=True)

    if source.source_type == "git":
        clone_dir = _git_clone_or_pull(source, cache_root)
        # Resolve the source folder: a top-level SKILL.md (path="") or a named subfolder.
        entry = next((s for s in _list_git_skills(clone_dir) if s["name"] == name), None)
        src_folder = clone_dir if (entry and entry["path"] == "") else (clone_dir / (entry["path"] if entry else safe))
        if not (src_folder / "SKILL.md").is_file():
            raise SkillInstallError(f"skill {name!r} not found in source {source.name!r}")
        dest = skills_dir / safe
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src_folder, dest)
        return {"ok": True, "name": safe, "path": str(dest)}

    if source.source_type == "local":
        base = Path(source.url)
        entry = next((s for s in _list_local_skills(source) if s["name"] == name), None)
        src_folder = base / (entry["path"] if entry else safe)
        if not (src_folder / "SKILL.md").is_file():
            raise SkillInstallError(f"skill {name!r} not found in source {source.name!r}")
        dest = skills_dir / safe
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src_folder, dest)
        return {"ok": True, "name": safe, "path": str(dest)}

    # http: fetch SKILL.md (+ listed files) into skills_dir/<name>/
    base = source.url.rstrip("/")
    entry = next((s for s in _list_http_skills(source) if s["name"] == name), None)
    if entry is None:
        raise SkillInstallError(f"skill {name!r} not found in source {source.name!r}")
    rel = entry["path"] or safe
    dest = skills_dir / safe
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    # The index entry may name a folder (path=foo) or the SKILL.md itself.
    md_url = f"{base}/{rel}/SKILL.md" if not rel.endswith(".md") else f"{base}/{rel}"
    (dest / "SKILL.md").write_text(_http_get(md_url), encoding="utf-8")
    return {"ok": True, "name": safe, "path": str(dest)}


def uninstall_skill(name: str, *, skills_dir: Path) -> dict[str, Any]:
    """Remove ``skills_dir/<name>/``. Raises if missing or protected as builtin."""
    safe = _safe_name(name)
    dest = skills_dir / safe
    if not dest.is_dir():
        raise SkillInstallError(f"skill {name!r} is not installed")
    # Builtin backstop: a SKILL.md frontmatter flag protects app-shipped skills from removal.
    md = dest / "SKILL.md"
    if md.is_file():
        text = md.read_text(encoding="utf-8")
        if text.startswith("---") and "x-openworker-builtin: true" in text.split("---", 2)[1]:
            raise SkillInstallError(f"skill {name!r} is built-in and cannot be uninstalled")
    shutil.rmtree(dest)
    return {"ok": True, "name": safe}
