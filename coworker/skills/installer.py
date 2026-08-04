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

    A failed ``pull --ff-only`` (exit 128 — common when the upstream force-pushed or the
    shallow clone diverged) is recovered by wiping the cache and re-cloning, rather than
    surfacing a hard error. This is what fixes "git pull returned non-zero exit status 128"
    on the Anthropic official skill source.
    """
    cache_dir = cache_root / source.id
    if cache_dir.is_dir() and (time.monotonic() - _clone_age(cache_dir)) < _CACHE_TTL:
        return cache_dir
    try:
        if cache_dir.is_dir():
            # Refresh in place. --ff-only avoids surprise merge commits on a cache we own.
            # On failure (exit 128 — upstream force-push / shallow divergence), wipe + re-clone
            # rather than leaving the user stuck on a permanently-broken source.
            try:
                subprocess.run(
                    ["git", "-C", str(cache_dir), "pull", "--ff-only", "--depth", "1"],
                    check=True, capture_output=True, timeout=60,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                shutil.rmtree(cache_dir, ignore_errors=True)
                cache_dir.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["git", "clone", "--depth", "1", source.url, str(cache_dir)],
                    check=True, capture_output=True, timeout=120,
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
    """Discover installable skills in a cloned repo: any folder containing a SKILL.md.

    Reads the SKILL.md frontmatter for name/description so the catalog matches what install
    will actually lay down. Supports three layouts:

    * Single-skill repo: ``SKILL.md`` at root.
    * Flat multi-skill: immediate subfolders each with a ``SKILL.md`` (e.g. ``pdf/SKILL.md``).
    * Nested multi-skill (Anthropic's layout): ``skills/<name>/SKILL.md`` — the real skills
      live under a ``skills/`` subdirectory, not at the top level. Without recursing into
      ``skills/``, the catalog only showed the ``template/`` placeholder and missed the real
      document skills (docx/pdf/pptx/xlsx).
    """
    from .base import _parse_skill  # reuse the existing frontmatter parser

    out: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    # Single-skill repo: SKILL.md at root.
    root_md = clone_dir / "SKILL.md"
    if root_md.is_file():
        try:
            sk = _parse_skill(root_md)
            out.append({"name": sk.name, "description": sk.description, "path": ""})
            seen_paths.add("")
        except Exception:
            pass

    # Multi-skill: walk for SKILL.md files. We look at immediate subfolders AND one level of
    # nesting (skills/<name>/SKILL.md) to cover both the flat and Anthropic layouts without
    # descending into vendored noise (node_modules, .git, etc.).
    def _scan_subfolders(parent: Path) -> None:
        if not parent.is_dir():
            return
        for sub in sorted(parent.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            md = sub / "SKILL.md"
            if md.is_file():
                rel = sub.relative_to(clone_dir).as_posix()
                if rel in seen_paths:
                    continue
                seen_paths.add(rel)
                try:
                    sk = _parse_skill(md)
                    out.append({"name": sk.name or sub.name, "description": sk.description, "path": rel})
                except Exception:
                    out.append({"name": sub.name, "description": "", "path": rel})

    # Immediate subfolders (flat layout).
    _scan_subfolders(clone_dir)
    # One level deeper (Anthropic layout: skills/<name>/SKILL.md).
    for sub in sorted(clone_dir.iterdir()):
        if sub.is_dir() and not sub.name.startswith(".") and sub.name != ".git":
            _scan_subfolders(sub)

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


# -- modelscope source (魔搭社区 — public PUT API, ~76k skills) ----------------
#
# The ModelScope skill center (https://www.modelscope.cn/skills) serves its catalog via a
# public same-origin API. The frontend SPA builds requests as PUT /api/v1/dolphin/skills
# with a JSON body {PageNumber, PageSize, Query, Criterion:[]} — no auth required. Each
# skill carries rich metadata (L1 category, SourceAvatar icon, DownloadCount, SourceURL to
# GitHub) and the detail endpoint GET /api/v1/skills/{Path}/{Name} returns ReadMeContent
# which is the full SKILL.md body. Reverse-engineered from the webpack bundle's request
# helper module (17799) which sets baseURL "/api" on www.modelscope.cn.

_MS_API_HOST = "https://www.modelscope.cn"
_MS_SKILLS_LIST = "/api/v1/dolphin/skills"
_MS_SKILL_DETAIL = "/api/v1/skills/{path}/{name}"


def _modelscope_request(url: str, *, method: str = "PUT", body: dict | None = None) -> dict:
    """One ModelScope API call. Returns the parsed JSON ``Data`` payload.

    Raises :class:`SkillInstallError` on network/HTTP/parse failure or a non-200 ``Code``.
    """
    try:
        client = httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True)
        if method == "PUT":
            resp = client.put(url, json=body or {})
        else:
            resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as e:
        raise SkillInstallError(f"ModelScope API request failed ({url}): {e}") from e
    except Exception as e:
        raise SkillInstallError(f"ModelScope API parse error ({url}): {e}") from e
    if not isinstance(payload, dict) or payload.get("Code") != 200:
        msg = payload.get("Message") if isinstance(payload, dict) else "unknown"
        raise SkillInstallError(f"ModelScope API error ({url}): Code={payload.get('Code') if isinstance(payload, dict) else '?'} {msg}")
    return payload.get("Data") or {}


def _list_modelscope_skills(
    source: SkillSource, *, page: int = 1, page_size: int = 100, query: str = ""
) -> dict[str, Any]:
    """Fetch one page of the ModelScope skill catalog.

    Returns ``{"skills": [...], "total_count": int, "page": int, "page_size": int,
    "categories": [str]}``. Each item is normalized to the catalog shape the UI expects,
    enriched with icon/category/downloads for the ModelScope card layout.
    """
    url = source.url or (_MS_API_HOST + _MS_SKILLS_LIST)
    body = {"PageNumber": page, "PageSize": page_size, "Query": query, "Criterion": []}
    data = _modelscope_request(url, method="PUT", body=body)
    raw_skills = data.get("SkillList") or []
    total = data.get("TotalCount") or 0
    items: list[dict[str, Any]] = []
    categories: set[str] = set()
    for s in raw_skills:
        if not isinstance(s, dict) or not s.get("Name"):
            continue
        l1 = s.get("L1") or {}
        cat = l1.get("ChineseName") or l1.get("Name") or ""
        if cat:
            categories.add(cat)
        items.append({
            "name": str(s.get("DisplayName") or s.get("Name") or ""),
            "description": str(s.get("Description") or s.get("DescriptionEn") or ""),
            "path": str(s.get("Path") or "") + "/" + str(s.get("Name") or ""),
            "category": cat,
            "icon": str(s.get("SourceAvatar") or ""),
            "downloads": int(s.get("DownloadCount") or 0),
            "author": str(s.get("SourceDeveloper") or s.get("Owner") or ""),
            "source_url": str(s.get("SourceURL") or ""),
            "tags": s.get("Tags") or [],
        })
    return {
        "skills": items,
        "total_count": total,
        "page": page,
        "page_size": page_size,
        "categories": sorted(categories),
    }


def _install_modelscope_skill(source: SkillSource, name: str, *, skills_dir: Path) -> dict[str, Any]:
    """Install a ModelScope skill by fetching its SKILL.md body from the detail API.

    The detail endpoint returns ``ReadMeContent`` (the full SKILL.md markdown). We write it
    to ``skills_dir/<name>/SKILL.md`` — progressive disclosure only needs name + description
    + body. Resource files (scripts/assets) are not fetched in this first version; the skill
    body alone is enough for the agent to use the skill's instructions.
    """
    safe = _safe_name(name.split("/")[-1])  # name may be "Path/Name" — take the leaf
    # The catalog item's ``path`` is "{Path}/{Name}". We need to split it back for the
    # detail endpoint: GET /api/v1/skills/{Path}/{Name}. Re-fetch the catalog page to find
    # the entry (cheaper than requiring the client to pass the path through).
    detail_path = None
    # Try the name as "Path/Name" first (how it's stored in catalog ``path``).
    if "/" in name:
        detail_path = name
    else:
        # Search the first page for a matching DisplayName/Name.
        catalog = _list_modelscope_skills(source, page=1, page_size=100, query=name)
        for it in catalog["skills"]:
            if it["name"] == name or it["path"].endswith("/" + name):
                detail_path = it["path"]
                break
    if not detail_path:
        raise SkillInstallError(f"skill {name!r} not found in ModelScope catalog")
    url = _MS_API_HOST + _MS_SKILL_DETAIL.format(path=detail_path.split("/")[0], name=detail_path.split("/", 1)[1])
    data = _modelscope_request(url, method="GET")
    readme = str(data.get("ReadMeContent") or "")
    if not readme:
        raise SkillInstallError(f"ModelScope skill {name!r} has no ReadMeContent")
    dest = skills_dir / safe
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(readme, encoding="utf-8")
    return {"ok": True, "name": safe, "path": str(dest)}


# -- skillhub source (腾讯 SkillHub — public GET search + ZIP download) --------
#
# SkillHub (https://skillhub.cn) is Tencent's Claude Code skill marketplace. Its
# public API (no auth) lives at api.skillhub.cn:
#   * search: GET /api/v1/search?q=<query>&size=<n> → {results: [{slug, displayName,
#     description_zh, category, icon_url, installs, stars, namespace:{canonicalName}, ...}]}
#   * download: GET /api/v1/download?slug=<slug> → 302 to a COS-hosted ZIP containing
#     SKILL.md (with frontmatter) + scripts/ + references/ + _meta.json + LICENSE.txt.
# Unlike the ModelScope installer (which only fetches the SKILL.md body), SkillHub
# ships the full resource bundle in the ZIP, so we extract everything — giving the
# agent access to bundled scripts and reference files (§6 progressive disclosure).

_SH_API_HOST = "https://api.skillhub.cn"
_SH_SEARCH = "/api/v1/search"
_SH_DOWNLOAD = "/api/v1/download"
_SH_CATEGORIES = "/api/v1/categories"


def _skillhub_get(url: str) -> Any:
    """One SkillHub API GET. Returns the parsed JSON payload (full body, not just ``Data``).

    Raises :class:`SkillInstallError` on network/HTTP/parse failure.
    """
    try:
        client = httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True)
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as e:
        raise SkillInstallError(f"SkillHub API request failed ({url}): {e}") from e
    except Exception as e:
        raise SkillInstallError(f"SkillHub API parse error ({url}): {e}") from e
    return payload


def _skillhub_categories(host: str = _SH_API_HOST) -> list[str]:
    """Fetch the 12 SkillHub category keys (e.g. ``office-efficiency``) from the dedicated
    endpoint. Best-effort — on failure returns an empty list (the page's own categories are
    still merged in by the caller).
    """
    try:
        payload = _skillhub_get(f"{host}{_SH_CATEGORIES}")
        items = payload.get("items") or [] if isinstance(payload, dict) else []
        return [str(c.get("key")) for c in items if isinstance(c, dict) and c.get("key")]
    except SkillInstallError:
        return []


def _list_skillhub_skills(
    source: SkillSource, *, page: int = 1, page_size: int = 100, query: str = ""
) -> dict[str, Any]:
    """Fetch one page of the SkillHub skill catalog via the search API.

    SkillHub's search endpoint (``GET /api/v1/search?q=&limit=N``) supports:

    * ``q``    — free-text search (works).
    * ``limit``— max results to return (works, **capped at 100** by the API; ``size`` is
                  silently ignored and always returns 10 — a footgun that left the catalog
                  showing only 10 skills for a long time).
    * ``page`` / ``offset`` / ``skip`` / ``from`` — all ignored; the endpoint always returns
      from the first match. So there is no real server-side pagination beyond the first 100.

    We request ``limit = min(page * page_size, 100)`` and slice the requested page out
    client-side. With the default ``page_size=100`` this surfaces the full first-100 window
    on page 1 (vs. the old 10). Categories come from the dedicated ``/api/v1/categories``
    endpoint (12 categories with Chinese names) so chips render on first load.

    Returns ``{"skills": [...], "total_count": int, "page": int, "page_size": int,
    "categories": [str]}`` — same shape as the ModelScope catalog so the UI's
    ``SkillBrowseSection`` handles both uniformly.
    """
    host = source.url or _SH_API_HOST
    # `limit` (not `size`) is the param the API honors; cap at 100 (the server's hard ceiling).
    fetch_limit = min(max(page * page_size, page_size), 100)
    url = f"{host}{_SH_SEARCH}?q={query or ''}&limit={fetch_limit}"
    payload = _skillhub_get(url)
    raw = payload.get("results") or [] if isinstance(payload, dict) else []
    total = len(raw)  # SkillHub doesn't return a separate total count; use result length.
    # Slice the requested page out of the fetched window.
    start = (page - 1) * page_size
    end = start + page_size
    page_items = raw[start:end]
    items: list[dict[str, Any]] = []
    categories: set[str] = set()
    for s in page_items:
        if not isinstance(s, dict) or not s.get("slug"):
            continue
        cat = str(s.get("category") or "")
        if cat:
            categories.add(cat)
        ns = s.get("namespace") or {}
        items.append({
            "name": str(s.get("slug") or ""),
            "display_name": str(s.get("displayName") or s.get("name") or ""),
            "description": str(s.get("description_zh") or s.get("description") or ""),
            "category": cat,
            "icon": str(s.get("icon_url") or ""),
            "downloads": int(s.get("installs") or s.get("downloads") or 0),
            "stars": int(s.get("stars") or 0),
            "author": str(ns.get("canonicalName") or s.get("owner_name") or ""),
            "version": str(s.get("version") or ""),
            "source_url": str(s.get("homepage") or f"https://skillhub.cn/skills/{s.get('slug', '')}"),
            "tags": s.get("tags") or [],
        })
    # Merge the page's categories with the full category catalog from the dedicated endpoint,
    # so chips render even when the current page doesn't span all categories.
    all_cats = _skillhub_categories(host)
    categories.update(all_cats)
    return {
        "skills": items,
        "total_count": total,
        "page": page,
        "page_size": page_size,
        "categories": sorted(categories),
    }


def _install_skillhub_skill(source: SkillSource, name: str, *, skills_dir: Path) -> dict[str, Any]:
    """Install a SkillHub skill by downloading its ZIP and extracting the full bundle.

    The download endpoint returns a ZIP with ``SKILL.md`` (frontmatter + body) plus
    ``scripts/``, ``references/``, and other bundled resources. We extract everything
    into ``skills_dir/<slug>/`` — richer than the ModelScope installer (which only
    writes SKILL.md) because SkillHub ships the complete resource bundle.
    """
    import io
    import zipfile

    safe = _safe_name(name.split("/")[-1])  # slug may contain a namespace prefix
    slug = name.split("/")[-1] if "/" in name else name
    host = source.url or _SH_API_HOST
    url = f"{host}{_SH_DOWNLOAD}?slug={slug}"
    try:
        client = httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True)
        resp = client.get(url)
        resp.raise_for_status()
        zip_bytes = resp.content
    except httpx.HTTPError as e:
        raise SkillInstallError(f"SkillHub download failed ({slug}): {e}") from e
    if not zip_bytes or zip_bytes[:2] != b"PK":
        raise SkillInstallError(f"SkillHub download for {slug!r} did not return a ZIP archive")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise SkillInstallError(f"SkillHub ZIP for {slug!r} is corrupt: {e}") from e
    # Verify the ZIP contains a SKILL.md (at root or one level down).
    names = zf.namelist()
    skill_md = next((n for n in names if n.rstrip("/").endswith("SKILL.md") and n.count("/") <= 1), None)
    if not skill_md:
        raise SkillInstallError(f"SkillHub ZIP for {slug!r} contains no SKILL.md")
    dest = skills_dir / safe
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    # Extract all files. Strip a single leading directory component if the ZIP nests
    # everything under a top-level folder (some packs do, SkillHub's don't — but be safe).
    for member in names:
        if member.endswith("/"):
            continue
        # Normalize the target path: strip leading "./" or a single top-level dir.
        rel = member
        if rel.startswith("./"):
            rel = rel[2:]
        parts = rel.split("/")
        # If there's a single top-level dir and SKILL.md is under it, strip that prefix.
        if skill_md.count("/") == 1 and len(parts) > 1 and parts[0] == skill_md.split("/")[0]:
            rel = "/".join(parts[1:])
        if not rel:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(member))
    return {"ok": True, "name": safe, "path": str(dest)}


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


def list_catalog(
    source: SkillSource, cache_root: Path, *, page: int = 1, page_size: int = 100, query: str = ""
) -> list[dict[str, Any]] | dict[str, Any]:
    """List installable skills from a source.

    For git/local/http sources returns a flat ``list[dict]`` (name + description + path).
    For ``modelscope`` sources returns a dict ``{skills, total_count, page, page_size,
    categories}`` — the catalog is ~76k items so it's paginated, not fetched in one shot.
    For ``skillhub`` sources returns the same dict shape (search API, client-side paginated).
    """
    if source.source_type == "modelscope":
        return _list_modelscope_skills(source, page=page, page_size=page_size, query=query)
    if source.source_type == "skillhub":
        return _list_skillhub_skills(source, page=page, page_size=page_size, query=query)
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

    if source.source_type == "modelscope":
        return _install_modelscope_skill(source, name, skills_dir=skills_dir)

    if source.source_type == "skillhub":
        return _install_skillhub_skill(source, name, skills_dir=skills_dir)

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
