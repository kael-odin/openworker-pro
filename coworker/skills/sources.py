"""Skill source management — mirrors DHP's ``digital_human/sources.py``.

A *skill source* is an HTTP index or a local directory that serves a catalog of installable
Anthropic SKILL.md skills. The index format is a small ``index.json``::

    {
      "skills": [
        {"name": "pdf", "description": "extract text from PDFs", "path": "pdf"},
        ...
      ]
    }

For an HTTP source, ``path`` is joined to the source URL and ``SKILL.md`` (+ any sibling
resource files listed in the index entry's ``files``) is fetched. For a local source, the
URL is a directory whose subfolders each contain a ``SKILL.md`` (the existing
:class:`~coworker.skills.base.SkillLoader` discovery layout) — install copies the folder.

Sources live in the manager prefs (``skill_sources`` key) so they survive restarts without a
separate store file. Builtin sources are re-asserted on every startup and cannot be deleted
— only disabled — mirroring the DHP empty-store guard.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class SkillSource:
    """One addressable skill catalog source."""

    id: str
    name: str
    url: str  # HTTP index URL, or a local dir path for source_type="local"
    enabled: bool = True
    is_default: bool = False
    source_type: str = "http"  # "http" (index.json) | "local" (dir of SKILL.md folders)

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
    def from_dict(cls, d: dict[str, Any]) -> "SkillSource":
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            url=str(d.get("url") or ""),
            enabled=bool(d.get("enabled", True)),
            is_default=bool(d.get("is_default", False)),
            source_type=str(d.get("source_type") or "http"),
        )


# Built-in skill sources. The Anthropic official skills repo is a GitHub repo of SKILL.md
# folders; we expose it as a local-clone-via-git source (the fetcher clones it on demand).
# A community HTTP index can be added here once one exists. Re-asserted on every startup so
# the source list is never empty — same guard as DHP's empty-store fix.
BUILTIN_SOURCES: list[SkillSource] = [
    SkillSource(
        id="anthropic-official",
        name="Anthropic 官方技能",
        url="https://github.com/anthropics/skills",
        enabled=True,
        is_default=True,
        source_type="git",  # cloned via git on demand
    ),
]


class SkillSourceManager:
    """Persists + serves the set of configured skill sources.

    Sources are stored under the ``skill_sources`` key of the manager prefs dict. The caller
    owns that dict and its save path (manager._load_prefs / _save_prefs); this class mutates
    the dict in place and calls ``save()`` so persistence is the caller's responsibility.
    """

    def __init__(self, prefs: dict[str, Any], save: Callable[[], None]) -> None:
        self._prefs = prefs
        self._save = save

    def _raw(self) -> list[dict[str, Any]]:
        raw = self._prefs.get("skill_sources")
        if not isinstance(raw, list):
            return []
        return raw

    def _write(self, sources: list[SkillSource]) -> None:
        self._prefs["skill_sources"] = [s.to_dict() for s in sources]
        self._save()

    def ensure_builtins(self) -> None:
        """Re-assert builtin sources. Idempotent; preserves user edits to a builtin's enabled flag."""
        sources = [SkillSource.from_dict(d) for d in self._raw()]
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
            sources.append(SkillSource(**builtin.__dict__))
        self._write(sources)

    def list(self, *, enabled_only: bool = False) -> list[SkillSource]:
        sources = [SkillSource.from_dict(d) for d in self._raw()]
        if not sources:
            sources = list(BUILTIN_SOURCES)
        if enabled_only:
            sources = [s for s in sources if s.enabled]
        sources.sort(key=lambda s: (not s.is_default, s.name.lower()))
        return sources

    def get(self, source_id: str) -> Optional[SkillSource]:
        for s in self.list():
            if s.id == source_id:
                return s
        return None

    def add(self, name: str, url: str, *, source_type: str = "http") -> SkillSource:
        name = (name or "").strip()
        url = (url or "").strip()
        if not name or not url:
            raise ValueError("name and url are required")
        sources = [SkillSource.from_dict(d) for d in self._raw()]
        src = SkillSource(
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

    def update(self, source_id: str, changes: dict[str, Any]) -> Optional[SkillSource]:
        sources = [SkillSource.from_dict(d) for d in self._raw()]
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
        sources = [SkillSource.from_dict(d) for d in self._raw()]
        target = next((s for s in sources if s.id == source_id), None)
        if target is None or target.is_default:
            return False  # builtins cannot be deleted — only disabled
        sources = [s for s in sources if s.id != source_id]
        self._write(sources)
        return True
