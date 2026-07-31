"""Persona marketplace browse + install — mirrors ``coworker/plugins/installer.py``.

A *persona marketplace* is a git repo whose tree contains persona manifests:
``*.md`` files with YAML frontmatter (see ``manifest.py``). There is no separate
catalog file (unlike plugins' ``marketplace.json``) — the manifests themselves
*are* the catalog. Browsing clones the repo and lists every parseable manifest;
installing one copies that single manifest into the managed install area via the
existing ``PersonaRegistry.install_from_dir`` path (so consent, snapshotting,
and the disabled-pending-approval lifecycle are reused verbatim).

Self-contained on purpose (its own ``_git_clone_or_pull``), matching the skills
installer's style, so the personas package stays decoupled from plugins/skills.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .manifest import ManifestError, parse_manifest
from .sources import PersonaSource

# Shared clone cache root. Marketplace clones live under
# <cache_root>/<source-id> so repeated catalog browses reuse the clone (git pull
# to refresh, throttled by _CACHE_TTL).
_CACHE_TTL = 600.0  # refresh a cached clone at most every 10 min


class PersonaMarketplaceError(Exception):
    """Raised when a persona marketplace cannot be browsed/installed (message
    surfaces to the UI)."""


# -- git helpers ---------------------------------------------------------------


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


def _git_clone_or_pull(url: str, cache_dir: Path) -> Path:
    """Clone (or refresh) ``url`` into ``cache_dir``. Shallow on first clone;
    refreshed via ``git pull`` only if the cache is older than _CACHE_TTL."""
    if cache_dir.is_dir() and (time.monotonic() - _clone_age(cache_dir)) < _CACHE_TTL:
        return cache_dir
    try:
        if cache_dir.is_dir():
            subprocess.run(
                ["git", "-C", str(cache_dir), "pull", "--ff-only", "--depth", "1"],
                check=True, capture_output=True, timeout=60,
            )
        else:
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(cache_dir)],
                check=True, capture_output=True, timeout=120,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise PersonaMarketplaceError(
            f"failed to clone persona source {url!r}: {e}"
        ) from e
    _mark_clone_age(cache_dir)
    return cache_dir


def _clone_sha(cache_dir: Path) -> str:
    """The cloned repo's HEAD commit sha (for update-check parity with plugins)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(cache_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip()
    except Exception:
        return ""


# -- manifest discovery -------------------------------------------------------


def _read_manifest_metadata(md_path: Path) -> Optional[dict[str, Any]]:
    """Parse a persona manifest for catalog display. Returns None (and is
    silently skipped) if the file isn't a valid persona manifest — a marketplace
    repo may contain README.md and other non-persona markdown that we must not
    list. We do *not* fully validate tools/recommends here (cheap browse); full
    validation runs at install via ``parse_manifest`` strictness."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    # parse_manifest enforces frontmatter + body + id charset + family enum +
    # tool validation. For a *browse* listing we want to surface entries even
    # when their tool list references capabilities we don't ship yet, so we
    # parse leniently: extract frontmatter metadata by hand and only require an
    # id (explicit or filename-derived) + a non-empty body.
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    import yaml

    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    body = text[end + 4:].lstrip("\n")
    if not body.strip():
        return None  # a manifest with no system prompt isn't installable
    import re

    from .manifest import _ID_RE, _slugify

    persona_id = str(meta.get("id") or "").strip()
    if persona_id:
        if not _ID_RE.match(persona_id):
            return None
    else:
        persona_id = _slugify(md_path.stem)
        if not persona_id:
            return None
    return {
        "id": persona_id,
        "name": str(meta.get("name") or persona_id).strip(),
        "tagline": str(meta.get("tagline") or "").strip(),
        "description": str(meta.get("description") or "").strip(),
        "icon": str(meta.get("icon", "")).strip(),
        "family": str(meta.get("family", "knowledge")).strip().lower(),
        # `file` (repo-relative path) is filled in by the caller (_scan_manifests),
        # which knows the clone root; we don't have it here.
    }


def _scan_manifests(clone_dir: Path) -> list[dict[str, Any]]:
    """Discover every persona manifest in a cloned marketplace repo.

    Scans all ``*.md`` files (excluding dotdirs like ``.git``). Each parseable
    manifest becomes one catalog entry carrying its repo-relative path so install
    can relocate exactly that file.
    """
    out: list[dict[str, Any]] = []
    for md in sorted(clone_dir.rglob("*.md")):
        # Skip anything inside a .git dir or other dotdir.
        if any(part.startswith(".") for part in md.relative_to(clone_dir).parts[:-1]):
            continue
        meta = _read_manifest_metadata(md)
        if meta is None:
            continue
        meta["file"] = md.relative_to(clone_dir).as_posix()
        out.append(meta)
    return out


# -- public API ----------------------------------------------------------------


def list_catalog(source: PersonaSource, cache_root: Path) -> list[dict[str, Any]]:
    """List installable personas from a marketplace source (clones on demand).

    Returns a list of normalized persona entries (id/name/tagline/description/
    icon/family/file). Each entry's ``file`` is the manifest's repo-relative
    path, which ``install_persona`` uses to fetch exactly that persona.
    """
    cache_dir = cache_root / source.id
    _git_clone_or_pull(source.url, cache_dir)
    return _scan_manifests(cache_dir)


def install_persona(
    source: PersonaSource,
    persona_id: str,
    *,
    registry,
    cache_root: Path,
) -> dict[str, Any]:
    """Install one persona ``persona_id`` from marketplace ``source``.

    Locates the manifest in the cloned marketplace by id (or filename stem),
    copies it into a throwaway dir, and delegates to
    ``registry.install_from_dir`` — reusing the consent-summary + snapshot +
    disabled-pending-approval lifecycle. Returns the consent summary the
    registry produces (a dict with id/name/tools/risk/...).
    """
    cache_dir = cache_root / source.id
    _git_clone_or_pull(source.url, cache_dir)
    catalog = _scan_manifests(cache_dir)

    entry = next((c for c in catalog if c["id"] == persona_id), None)
    if entry is None:
        raise PersonaMarketplaceError(
            f"persona {persona_id!r} not found in marketplace {source.name!r}"
        )

    src_file = cache_dir / entry["file"]
    if not src_file.is_file():
        raise PersonaMarketplaceError(
            f"persona {persona_id!r}: manifest file {entry['file']!r} missing from clone"
        )

    # install_from_dir installs *every* *.md in the dir. To install exactly one
    # persona, copy its manifest into a clean temp dir and install that. This
    # reuses the full parse_manifest strictness (tool/recommends validation),
    # the snapshotting into the managed area, and the disabled-pending-consent
    # lifecycle — the marketplace never changes the trust model.
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / f"{persona_id}.md"
        shutil.copy2(src_file, dest)
        summaries = registry.install_from_dir(td)

    if not summaries:
        raise PersonaMarketplaceError(
            f"persona {persona_id!r}: install produced no consent summary (manifest invalid?)"
        )
    return {"ok": True, "consent": summaries[0], "source_id": source.id}


def uninstall_persona(persona_id: str, *, registry) -> dict[str, Any]:
    """Remove an installed marketplace persona. Thin wrapper around the
    registry's uninstall (which already guards builtins and clears state)."""
    registry.uninstall(persona_id)
    return {"ok": True, "id": persona_id}
