"""DHP registry store — resolve digital humans from a local DHP repo clone or the official index.

Two sources, one API:

* **Local repo** — a clone of ``openkursar/digital-human-protocol`` on disk. The registry reads
  ``index.json`` for the catalog (slug / name / description / category / tags / icon / i18n), then
  lazily parses ``packages/digital-humans/<slug>/spec.yaml`` on demand via :mod:`.spec`. This is the
  zero-network path used in dev and by users who keep the repo cloned.
* **Remote index** — the same ``index.json`` served at the DHP GitHub Pages URL. Used for listing
  when no local repo is configured; spec fetch then requires a per-slug download (future; the local
  repo is the supported path today).

The registry never holds full specs in memory — only the lightweight index entries. Specs are
parsed on demand and cached by slug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .spec import DigitalHumanSpec, SpecError, load_spec_file

# The official DHP registry index, served via GitHub Pages.
OFFICIAL_INDEX_URL = "https://openkursar.github.io/digital-human-protocol/index.json"

# Categories per spec/categories.md — used for grouping in the store UI. Free-form categories from
# the index are preserved; this list drives the canonical group order.
CATEGORY_ORDER = (
    "content", "social", "productivity", "dev-tools", "monitoring",
    "commerce", "finance", "communication", "other",
)


@dataclass
class RegistryEntry:
    """One row of the DHP ``index.json`` ``apps`` array — the catalog listing without the full spec."""

    slug: str
    name: str
    version: str = ""
    author: str = ""
    description: str = ""
    type: str = "automation"
    category: str = ""
    tags: list[str] = field(default_factory=list)
    icon: str = ""
    locale: str = ""
    min_app_version: str = ""
    path: str = ""  # repo-relative path to the package dir
    checksum: str = ""
    size_bytes: int = 0
    i18n: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "type": self.type,
            "category": self.category,
            "tags": list(self.tags),
            "icon": self.icon,
            "locale": self.locale,
            "min_app_version": self.min_app_version,
            "updated_at": self.updated_at,
            "has_i18n": bool(self.i18n),
        }


def _entry_from_index(item: dict[str, Any]) -> RegistryEntry:
    return RegistryEntry(
        slug=str(item.get("slug") or "").strip(),
        name=str(item.get("name") or "").strip(),
        version=str(item.get("version") or "").strip(),
        author=str(item.get("author") or "").strip(),
        description=str(item.get("description") or "").strip(),
        type=str(item.get("type") or "automation").strip(),
        category=str(item.get("category") or "").strip(),
        tags=[str(t) for t in (item.get("tags") or []) if isinstance(t, str)],
        icon=str(item.get("icon") or "").strip(),
        locale=str(item.get("locale") or "").strip(),
        min_app_version=str(item.get("min_app_version") or "").strip(),
        path=str(item.get("path") or "").strip(),
        checksum=str(item.get("checksum") or "").strip(),
        size_bytes=int(item.get("size_bytes") or 0),
        i18n=item.get("i18n") if isinstance(item.get("i18n"), dict) else {},
        updated_at=str(item.get("updated_at") or "").strip(),
    )


class DhpRegistry:
    """Resolves DHP catalog entries + specs from a local repo clone (and, in future, the remote index).

    Pass ``repo_dir`` = the root of a ``digital-human-protocol`` checkout (the dir containing
    ``index.json`` and ``packages/``). The registry reads ``index.json`` eagerly (it's small) and
    parses specs lazily.
    """

    def __init__(self, repo_dir: Optional[str | Path] = None) -> None:
        self.repo_dir = Path(repo_dir) if repo_dir else None
        self._entries: dict[str, RegistryEntry] = {}
        self._spec_cache: dict[str, DigitalHumanSpec] = {}
        if self.repo_dir is not None:
            self._load_index(self.repo_dir / "index.json")

    # -- loading ----------------------------------------------------------------
    def _load_index(self, index_path: Path) -> None:
        if not index_path.is_file():
            return
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in data.get("apps") or []:
            if not isinstance(item, dict):
                continue
            entry = _entry_from_index(item)
            if entry.slug:
                self._entries[entry.slug] = entry

    # -- queries ----------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    def slugs(self) -> list[str]:
        return sorted(self._entries)

    def list(self, *, category: Optional[str] = None) -> list[RegistryEntry]:
        """Catalog entries, optionally filtered by category. Sorted by category order then name."""
        entries = list(self._entries.values())
        if category:
            entries = [e for e in entries if e.category == category]
        entries.sort(key=lambda e: (_cat_rank(e.category), e.name.lower()))
        return entries

    def categories(self) -> list[str]:
        """Distinct categories present, in canonical order."""
        present = {e.category for e in self._entries.values() if e.category}
        return [c for c in CATEGORY_ORDER if c in present] + sorted(present - set(CATEGORY_ORDER))

    def get(self, slug: str) -> Optional[RegistryEntry]:
        return self._entries.get(slug)

    def get_spec(self, slug: str) -> DigitalHumanSpec:
        """Parse and return the full spec for ``slug``. Raises :class:`SpecError` if the slug is
        unknown or the spec is malformed."""
        if slug in self._spec_cache:
            return self._spec_cache[slug]
        entry = self._entries.get(slug)
        if entry is None or self.repo_dir is None:
            raise SpecError(f"digital human {slug!r} is not in the registry")
        # Resolve the package dir: prefer the index `path`, fall back to the conventional location.
        pkg_dir = self.repo_dir / entry.path if entry.path else self.repo_dir / "packages" / "digital-humans" / slug
        spec_path = pkg_dir / "spec.yaml"
        if not spec_path.is_file():
            # Some packages might use spec.json.
            alt = pkg_dir / "spec.json"
            if alt.is_file():
                spec_path = alt
            else:
                raise SpecError(f"no spec.yaml found for {slug!r} at {pkg_dir}")
        spec = load_spec_file(spec_path)
        self._spec_cache[slug] = spec
        return spec

    def reload(self) -> None:
        """Drop caches and re-read the index (e.g. after a ``git pull`` of the repo)."""
        self._entries.clear()
        self._spec_cache.clear()
        if self.repo_dir is not None:
            self._load_index(self.repo_dir / "index.json")


def _cat_rank(category: str) -> int:
    try:
        return CATEGORY_ORDER.index(category)
    except ValueError:
        return len(CATEGORY_ORDER)
