"""Registry source management for DHP digital-human stores.

A *source* is an HTTP index (or a local repo clone) that serves a DHP ``index.json`` catalog plus
per-slug ``spec.yaml`` files. Halo calls these "registries"; here we call them sources to avoid
confusion with the in-process :class:`~coworker.digital_human.store.DhpRegistry`.

Sources live in the manager prefs (``dhp_sources`` key) so they survive restarts without a separate
store file. Builtin sources (the official DHP index) are re-asserted on every startup and cannot be
deleted — a user can disable one, but not remove it, so the store is never accidentally emptied
(which is the root cause of the empty-store bug: a single local-clone source that vanished).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .store import OFFICIAL_INDEX_URL


@dataclass
class RegistrySource:
    """One addressable DHP catalog source."""

    id: str
    name: str
    url: str  # HTTP index URL, or a local dir path for source_type="local"
    enabled: bool = True
    is_default: bool = False
    source_type: str = "dhp"  # "dhp" (HTTP) | "local" (repo clone)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "enabled": self.enabled,
            "is_default": self.is_default,
            "source_type": self.source_type,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegistrySource":
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            url=str(d.get("url") or ""),
            enabled=bool(d.get("enabled", True)),
            is_default=bool(d.get("is_default", False)),
            source_type=str(d.get("source_type") or "dhp"),
        )


# The official DHP registry index, served via GitHub Pages. Re-asserted on every startup so the
# store is never empty even with no user configuration — this fixes the empty-store bug at its root.
# Points to the fully-localized Chinese fork so preset agents show Chinese names by default.
BUILTIN_SOURCES: list[RegistrySource] = [
    RegistrySource(
        id="dhp-official",
        name="数字人市场（官方）",
        url=OFFICIAL_INDEX_URL,
        enabled=True,
        is_default=True,
        source_type="dhp",
    ),
]


class SourceManager:
    """Persists + serves the set of configured DHP sources.

    Sources are stored under the ``dhp_sources`` key of the manager prefs dict. The caller owns
    that dict and its save path (manager._load_prefs / _save_prefs); this class mutates the dict
    in place and calls ``save()`` so persistence is the caller's responsibility, not ours.
    """

    def __init__(self, prefs: dict[str, Any], save: Callable[[], None]) -> None:
        self._prefs = prefs
        self._save = save

    def _raw(self) -> list[dict[str, Any]]:
        raw = self._prefs.get("dhp_sources")
        if not isinstance(raw, list):
            return []
        return raw

    def _write(self, sources: list[RegistrySource]) -> None:
        self._prefs["dhp_sources"] = [s.to_dict() for s in sources]
        self._save()

    def ensure_builtins(self) -> None:
        """Re-assert builtin sources. Idempotent; preserves user edits to a builtin's enabled flag.

        A builtin that the user disabled stays disabled — we only add missing builtins and refresh
        their name/url (in case OFFICIAL_INDEX_URL changed between versions)."""
        sources = [RegistrySource.from_dict(d) for d in self._raw()]
        seen_ids = {s.id for s in sources}
        for builtin in BUILTIN_SOURCES:
            if builtin.id in seen_ids:
                # Refresh mutable metadata (name/url) but keep the user's enabled preference.
                for s in sources:
                    if s.id == builtin.id:
                        s.name = builtin.name
                        s.url = builtin.url
                        s.is_default = True
                        s.source_type = builtin.source_type
                        break
                continue
            sources.append(RegistrySource(**builtin.__dict__))
        self._write(sources)

    def list(self, *, enabled_only: bool = False) -> list[RegistrySource]:
        sources = [RegistrySource.from_dict(d) for d in self._raw()]
        # If ensure_builtins was never called (fresh prefs), seed from BUILTIN_SOURCES.
        if not sources:
            sources = list(BUILTIN_SOURCES)
        if enabled_only:
            sources = [s for s in sources if s.enabled]
        # Default source first, then alphabetical by name.
        sources.sort(key=lambda s: (not s.is_default, s.name.lower()))
        return sources

    def get_default(self) -> Optional[RegistrySource]:
        for s in self.list():
            if s.is_default:
                return s
        return None

    def add(self, name: str, url: str, *, source_type: str = "dhp") -> RegistrySource:
        name = (name or "").strip()
        url = (url or "").strip()
        if not name or not url:
            raise ValueError("name and url are required")
        sources = [RegistrySource.from_dict(d) for d in self._raw()]
        src = RegistrySource(
            id="src-" + uuid.uuid4().hex[:10],
            name=name,
            url=url,
            enabled=True,
            is_default=False,
            source_type=source_type,
        )
        sources.append(src)
        self._write(sources)
        return src

    def update(self, source_id: str, changes: dict[str, Any]) -> Optional[RegistrySource]:
        sources = [RegistrySource.from_dict(d) for d in self._raw()]
        updated = None
        for s in sources:
            if s.id == source_id:
                if "name" in changes:
                    s.name = str(changes["name"]).strip() or s.name
                if "url" in changes:
                    s.url = str(changes["url"]).strip() or s.url
                if "enabled" in changes:
                    s.enabled = bool(changes["enabled"])
                if "source_type" in changes:
                    s.source_type = str(changes["source_type"]) or s.source_type
                updated = s
                break
        if updated is None:
            return None
        self._write(sources)
        return updated

    def remove(self, source_id: str) -> bool:
        sources = [RegistrySource.from_dict(d) for d in self._raw()]
        target = next((s for s in sources if s.id == source_id), None)
        if target is None:
            return False
        if target.is_default:
            # Builtins cannot be deleted — only disabled. This is the guard that prevents the
            # empty-store bug from recurring: the official source is always present.
            return False
        sources = [s for s in sources if s.id != source_id]
        self._write(sources)
        return True
