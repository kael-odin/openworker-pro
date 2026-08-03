"""Plugin source management — mirrors ``coworker/skills/sources.py``.

A *plugin source* is a git marketplace repository that serves a Claude Code
``.claude-plugin/marketplace.json`` catalog. The catalog lists installable plugins;
each entry carries a ``source`` object (``git-subdir`` / ``url`` / ``github`` / a
local string path relative to the marketplace repo) that tells the installer where
to fetch the plugin's actual code.

Sources live in the manager prefs (``plugin_sources`` key) so they survive restarts
without a separate store file. Builtin sources are re-asserted on every startup and
cannot be deleted — only disabled — mirroring the DHP / skill empty-store guard.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class PluginSource:
    """One addressable plugin marketplace source."""

    id: str
    name: str
    url: str  # git URL of the marketplace repo (cloned on demand)
    enabled: bool = True
    is_default: bool = False
    source_type: str = "git"  # only "git" marketplaces supported (marketplace.json layout)

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
    def from_dict(cls, d: dict[str, Any]) -> "PluginSource":
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            url=str(d.get("url") or ""),
            enabled=bool(d.get("enabled", True)),
            is_default=bool(d.get("is_default", False)),
            source_type=str(d.get("source_type") or "git"),
        )


# Built-in plugin marketplaces. The Anthropic official plugins repo is a git marketplace
# whose .claude-plugin/marketplace.json lists ~100+ plugins across categories (security,
# development, database, productivity, ...). Re-asserted on every startup so the source
# list is never empty — same guard as DHP / skills.
#
# ModelScope (魔搭社区) skills repo ships a Claude-Code-compatible
# .claude-plugin/marketplace.json listing ms-hub / ms-studio-deploy — the ModelScope
# skill center (https://www.modelscope.cn/skills) mirrors this catalog. Added as a second
# default so the 魔搭 skills appear out-of-the-box alongside the Anthropic catalog.
BUILTIN_SOURCES: list[PluginSource] = [
    PluginSource(
        id="claude-official",
        name="Claude 官方插件市场",
        url="https://github.com/anthropics/claude-plugins-official.git",
        enabled=True,
        is_default=True,
        source_type="git",
    ),
    PluginSource(
        id="modelscope-skills",
        name="魔搭社区技能中心",
        url="https://github.com/modelscope/modelscope-skills.git",
        enabled=True,
        is_default=True,
        source_type="git",
    ),
]


class PluginSourceManager:
    """Persists + serves the set of configured plugin marketplace sources.

    Sources are stored under the ``plugin_sources`` key of the manager prefs dict. The
    caller owns that dict and its save path (manager._load_prefs / _save_prefs); this
    class mutates the dict in place and calls ``save()`` so persistence is the caller's
    responsibility.
    """

    def __init__(self, prefs: dict[str, Any], save: Callable[[], None]) -> None:
        self._prefs = prefs
        self._save = save

    def _raw(self) -> list[dict[str, Any]]:
        raw = self._prefs.get("plugin_sources")
        if not isinstance(raw, list):
            return []
        return raw

    def _write(self, sources: list[PluginSource]) -> None:
        self._prefs["plugin_sources"] = [s.to_dict() for s in sources]
        self._save()

    def _deleted_builtins(self) -> set[str]:
        raw = self._prefs.get("deleted_builtin_plugin_sources")
        if not isinstance(raw, list):
            return set()
        return {str(x) for x in raw}

    def _write_deleted_builtins(self, ids: set[str]) -> None:
        self._prefs["deleted_builtin_plugin_sources"] = sorted(ids)
        self._save()

    def ensure_builtins(self) -> None:
        """Re-assert builtin sources — *unless* the user has explicitly deleted one.

        Idempotent; preserves user edits to a builtin's enabled flag. A builtin that was
        deleted (via ``remove``) stays deleted across restarts because it's recorded in the
        ``deleted_builtin_plugin_sources`` pref — the user chose "deleted means deleted, no
        auto-restore".
        """
        deleted = self._deleted_builtins()
        sources = [PluginSource.from_dict(d) for d in self._raw()]
        seen_ids = {s.id for s in sources}
        for builtin in BUILTIN_SOURCES:
            if builtin.id in deleted:
                continue  # user deleted this builtin — don't re-assert
            if builtin.id in seen_ids:
                for s in sources:
                    if s.id == builtin.id:
                        s.name = builtin.name
                        s.url = builtin.url
                        s.is_default = True
                        s.source_type = builtin.source_type
                        break
                continue
            sources.append(PluginSource(**builtin.__dict__))
        self._write(sources)

    def list(self, *, enabled_only: bool = False) -> list[PluginSource]:
        sources = [PluginSource.from_dict(d) for d in self._raw()]
        # The BUILTIN_SOURCES fallback only applies on a truly fresh state (no prefs key at
        # all). Once ensure_builtins() has run, an empty list means the user deleted every
        # source — don't resurrect builtins that were explicitly removed.
        if not sources and not self._raw() and not self._deleted_builtins():
            sources = list(BUILTIN_SOURCES)
        if enabled_only:
            sources = [s for s in sources if s.enabled]
        sources.sort(key=lambda s: (not s.is_default, s.name.lower()))
        return sources

    def get(self, source_id: str) -> Optional[PluginSource]:
        for s in self.list():
            if s.id == source_id:
                return s
        return None

    def add(self, name: str, url: str, *, source_type: str = "git") -> PluginSource:
        name = (name or "").strip()
        url = (url or "").strip()
        if not name or not url:
            raise ValueError("name and url are required")
        sources = [PluginSource.from_dict(d) for d in self._raw()]
        src = PluginSource(
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

    def update(self, source_id: str, changes: dict[str, Any]) -> Optional[PluginSource]:
        sources = [PluginSource.from_dict(d) for d in self._raw()]
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
        sources = [PluginSource.from_dict(d) for d in self._raw()]
        target = next((s for s in sources if s.id == source_id), None)
        if target is None:
            return False
        # All sources are deletable, including builtins. A deleted builtin is recorded so
        # ensure_builtins() doesn't re-assert it on the next startup — "deleted means deleted".
        if target.is_default:
            deleted = self._deleted_builtins()
            deleted.add(target.id)
            self._write_deleted_builtins(deleted)
        sources = [s for s in sources if s.id != source_id]
        self._write(sources)
        return True
