"""DHP registry store — resolve digital humans from one or more configured sources.

A *source* (see :mod:`.sources`) is either an HTTP index (the default — the DHP GitHub Pages site)
or a local ``digital-human-protocol`` checkout. The registry aggregates every enabled source:
``list`` merges their catalog entries, ``get_spec`` locates which source owns a slug and fetches
its spec through that source's adapter (see :mod:`.adapters`).

This multi-source design fixes the empty-store bug at its root: the default HTTP source is always
present (re-asserted on startup), so the store is never empty even when no local clone exists and
no ``OPENWORKER_DHP_REPO`` env is set — which was the sidecar-restart failure mode.

For backward compatibility (and dev/tests that pass a repo dir directly), the single-``repo_dir``
constructor still works; it builds a single local source under the hood.
"""

from __future__ import annotations

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
    name: str = ""
    version: str = ""
    author: str = ""
    description: str = ""
    type: str = "automation"
    category: str = ""
    tags: list[str] = field(default_factory=list)
    icon: str = ""
    locale: str = ""
    min_app_version: str = ""
    path: str = ""  # source-relative path to the package dir
    checksum: str = ""
    size_bytes: int = 0
    i18n: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""
    source_id: str = ""  # which source owns this entry

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
    """Resolves DHP catalog entries + specs across one or more sources.

    Pass a list of :class:`~coworker.digital_human.sources.RegistrySource` (the production path —
    the manager builds these from SourceManager) OR a single ``repo_dir`` (the legacy/dev path).
    Sources are resolved lazily through adapters; a source that fails to load yields no entries
    rather than crashing the whole store.
    """

    def __init__(self, sources=None, *, repo_dir: Optional[str | Path] = None) -> None:
        # source_id → adapter. Adapters are built lazily on first use (so a misconfigured HTTP
        # source doesn't fail at startup — it fails when first queried, and only for that source).
        self._adapters: dict[str, Any] = {}
        self._entry_index: dict[str, tuple[str, RegistryEntry]] = {}  # slug → (source_id, entry)
        self._loaded = False
        self._spec_cache: dict[str, DigitalHumanSpec] = {}

        # Backward-compat: the legacy constructor was DhpRegistry(repo_dir) — a positional path.
        # Detect a str/Path passed as the first arg and treat it as repo_dir.
        if sources is not None and not _is_source_list(sources):
            repo_dir = sources
            sources = None

        if sources is not None:
            # Multi-source path. Import here to avoid a circular import at module load.
            from .adapters import make_adapter

            self._sources = list(sources)
            for src in self._sources:
                if not src.enabled:
                    continue
                adapter = make_adapter(src)
                if adapter is not None:
                    self._adapters[src.id] = adapter
        else:
            # Legacy single-repo path (tests, dev with OPENWORKER_DHP_REPO).
            from .adapters import LocalRepoAdapter

            self._sources = []
            if repo_dir is not None:
                repo_path = Path(repo_dir)
                if repo_path.is_dir():
                    adapter = LocalRepoAdapter(repo_path)
                    self._adapters["local"] = adapter
                    self._sources.append(_LegacyLocalSource(str(repo_path)))

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._entry_index.clear()
        # Default-source entries take precedence on slug collision (first source in list wins).
        # We iterate in the source order given (SourceManager sorts default-first).
        for source_id, adapter in self._adapters.items():
            try:
                entries = adapter.fetch_index()
            except Exception:
                continue
            for entry in entries:
                if entry.slug and entry.slug not in self._entry_index:
                    entry.source_id = source_id
                    self._entry_index[entry.slug] = (source_id, entry)

    # -- queries ----------------------------------------------------------------
    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._entry_index)

    def slugs(self) -> list[str]:
        self._ensure_loaded()
        return sorted(self._entry_index)

    def list(self, *, category: Optional[str] = None) -> list[RegistryEntry]:
        """Catalog entries, optionally filtered by category. Sorted by category order then name."""
        self._ensure_loaded()
        entries = [e for _, e in self._entry_index.values()]
        if category:
            entries = [e for e in entries if e.category == category]
        entries.sort(key=lambda e: (_cat_rank(e.category), e.name.lower()))
        return entries

    def categories(self) -> list[str]:
        """Distinct categories present, in canonical order."""
        self._ensure_loaded()
        present = {e.category for _, e in self._entry_index.values() if e.category}
        return [c for c in CATEGORY_ORDER if c in present] + sorted(present - set(CATEGORY_ORDER))

    def get(self, slug: str) -> Optional[RegistryEntry]:
        self._ensure_loaded()
        pair = self._entry_index.get(slug)
        return pair[1] if pair else None

    def get_spec(self, slug: str) -> DigitalHumanSpec:
        """Parse and return the full spec for ``slug``. Raises :class:`SpecError` if unknown."""
        if slug in self._spec_cache:
            return self._spec_cache[slug]
        self._ensure_loaded()
        pair = self._entry_index.get(slug)
        if pair is None:
            raise SpecError(f"digital human {slug!r} is not in the registry")
        source_id, entry = pair
        adapter = self._adapters.get(source_id)
        if adapter is None:
            raise SpecError(f"source {source_id!r} for {slug!r} is not available")
        spec = adapter.fetch_spec(slug, entry.path or f"packages/digital-humans/{slug}/spec.yaml")
        self._spec_cache[slug] = spec
        return spec

    def reload(self) -> None:
        """Drop caches and re-fetch indexes (e.g. after a source is added/toggled)."""
        for adapter in self._adapters.values():
            try:
                adapter.reload()
            except Exception:
                pass
        self._entry_index.clear()
        self._spec_cache.clear()
        self._loaded = False


@dataclass
class _LegacyLocalSource:
    """A minimal source-like object for the legacy ``repo_dir`` constructor."""

    url: str
    enabled: bool = True
    is_default: bool = True
    source_type: str = "local"
    id: str = "local"
    name: str = "本地 DHP 仓库"


def _cat_rank(category: str) -> int:
    try:
        return CATEGORY_ORDER.index(category)
    except ValueError:
        return len(CATEGORY_ORDER)


def _is_source_list(value) -> bool:
    """True if ``value`` is a list/iterable of RegistrySource-like objects (vs a bare path string).

    Used to disambiguate the legacy ``DhpRegistry(repo_dir)`` positional call from the multi-source
    ``DhpRegistry(sources_list)`` call — a str/Path arg is repo_dir, a list is sources.
    """
    if isinstance(value, (str, Path, bytes)):
        return False
    if isinstance(value, (list, tuple)):
        return all(hasattr(item, "source_type") for item in value) if value else True
    # A generator of RegistrySource objects — accept it.
    return hasattr(value, "__iter__")
