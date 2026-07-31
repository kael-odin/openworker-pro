"""Persona marketplace source management — mirrors ``coworker/plugins/sources.py``.

A *persona source* is a git repository whose tree contains persona manifests
(``*.md`` files with YAML frontmatter, see ``manifest.py``). Browsing the source
clones it on demand and lists every manifest it finds; installing one copies
that single manifest into the managed install area via the existing
``PersonaRegistry.install_from_dir`` path.

Sources live in the manager prefs (``persona_sources`` key) so they survive
restarts without a separate store file. Unlike plugins / skills / DHP, there is
no Anthropic-official persona marketplace today, so ``BUILTIN_SOURCES`` starts
empty — the user adds their own. The empty-store guard still applies: a builtin
that we do ship later could not be deleted, only disabled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class PersonaSource:
    """One addressable persona marketplace source (a git repo of *.md manifests)."""

    id: str
    name: str
    url: str  # git URL of the marketplace repo (cloned on demand)
    enabled: bool = True
    is_default: bool = False
    source_type: str = "git"  # only "git" marketplaces supported (*.md tree layout)

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
    def from_dict(cls, d: dict[str, Any]) -> "PersonaSource":
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            url=str(d.get("url") or ""),
            enabled=bool(d.get("enabled", True)),
            is_default=bool(d.get("is_default", False)),
            source_type=str(d.get("source_type") or "git"),
        )


# Built-in persona marketplaces. None today — there is no Anthropic-official
# persona marketplace yet, so the user supplies their own sources. Re-asserted
# on every startup (ensure_builtins is a no-op when this is empty) so adding one
# later is just a matter of appending here; the empty-store guard (builtins
# cannot be deleted, only disabled) then applies automatically.
BUILTIN_SOURCES: list[PersonaSource] = []


class PersonaSourceManager:
    """Persists + serves the set of configured persona marketplace sources.

    Sources are stored under the ``persona_sources`` key of the manager prefs
    dict. The caller owns that dict and its save path (manager._load_prefs /
    _save_prefs); this class mutates the dict in place and calls ``save()`` so
    persistence is the caller's responsibility.
    """

    def __init__(self, prefs: dict[str, Any], save: Callable[[], None]) -> None:
        self._prefs = prefs
        self._save = save

    def _raw(self) -> list[dict[str, Any]]:
        raw = self._prefs.get("persona_sources")
        if not isinstance(raw, list):
            return []
        return raw

    def _write(self, sources: list[PersonaSource]) -> None:
        self._prefs["persona_sources"] = [s.to_dict() for s in sources]
        self._save()

    def ensure_builtins(self) -> None:
        """Re-assert builtin sources. Idempotent; preserves user edits to a
        builtin's enabled flag. A no-op today (BUILTIN_SOURCES is empty)."""
        sources = [PersonaSource.from_dict(d) for d in self._raw()]
        seen_ids = {s.id for s in sources}
        for builtin in BUILTIN_SOURCES:
            if builtin.id in seen_ids:
                for s in sources:
                    if s.id == builtin.id:
                        s.name = builtin.name
                        s.url = builtin.url
                        s.is_default = True
                        s.source_type = builtin.source_type
                        break
                continue
            sources.append(PersonaSource(**builtin.__dict__))
        self._write(sources)

    def list(self, *, enabled_only: bool = False) -> list[PersonaSource]:
        sources = [PersonaSource.from_dict(d) for d in self._raw()]
        if enabled_only:
            sources = [s for s in sources if s.enabled]
        # Default source first, then alphabetical by name.
        sources.sort(key=lambda s: (not s.is_default, s.name.lower()))
        return sources

    def get(self, source_id: str) -> Optional[PersonaSource]:
        for s in self.list():
            if s.id == source_id:
                return s
        return None

    def add(self, name: str, url: str, *, source_type: str = "git") -> PersonaSource:
        name = (name or "").strip()
        url = (url or "").strip()
        if not name or not url:
            raise ValueError("name and url are required")
        sources = [PersonaSource.from_dict(d) for d in self._raw()]
        src = PersonaSource(
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

    def update(self, source_id: str, changes: dict[str, Any]) -> Optional[PersonaSource]:
        sources = [PersonaSource.from_dict(d) for d in self._raw()]
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
        sources = [PersonaSource.from_dict(d) for d in self._raw()]
        target = next((s for s in sources if s.id == source_id), None)
        if target is None or target.is_default:
            return False  # builtins cannot be deleted — only disabled
        sources = [s for s in sources if s.id != source_id]
        self._write(sources)
        return True
