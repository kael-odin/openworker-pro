"""Installed-plugin registry — persists which plugins are installed + their components.

Mirrors the prefs-persisted store pattern of ``SkillSourceManager`` / ``RuleStore``.
The registry is the source of truth for "what's installed" — it records each plugin's
name, version, origin (source_id + source_info), the components it contributed
(skills / commands / mcps), and the pinned sha. Uninstall uses ``components.mcps`` to
reverse-register any MCP servers the plugin added.

Stored under the ``installed_plugins`` key of the manager prefs dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class InstalledPlugin:
    """One installed plugin entry."""

    name: str
    version: str = ""
    description: str = ""
    source_id: str = ""  # the marketplace source it was installed from
    source_info: dict[str, Any] = field(default_factory=dict)  # raw marketplace `source` object
    components: dict[str, list[str]] = field(default_factory=dict)  # {skills:[], commands:[], mcps:[]}
    installed_at: str = ""  # ISO timestamp set by caller
    sha: str = ""  # pinned commit sha at install time (for update checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source_id": self.source_id,
            "source_info": dict(self.source_info),
            "components": {k: list(v) for k, v in self.components.items()},
            "installed_at": self.installed_at,
            "sha": self.sha,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InstalledPlugin":
        comps = d.get("components") or {}
        return cls(
            name=str(d.get("name") or ""),
            version=str(d.get("version") or ""),
            description=str(d.get("description") or ""),
            source_id=str(d.get("source_id") or ""),
            source_info=dict(d.get("source_info") or {}),
            components={k: list(v) for k, v in (comps.items() if isinstance(comps, dict) else [])},
            installed_at=str(d.get("installed_at") or ""),
            sha=str(d.get("sha") or ""),
        )


class PluginRegistry:
    """Persists the set of installed plugins under the ``installed_plugins`` prefs key.

    The caller owns the prefs dict + save callback (manager._load_prefs / _save_prefs);
    this class mutates the dict in place and calls ``save()``.
    """

    def __init__(self, prefs: dict[str, Any], save: Callable[[], None]) -> None:
        self._prefs = prefs
        self._save = save

    def _raw(self) -> list[dict[str, Any]]:
        raw = self._prefs.get("installed_plugins")
        if not isinstance(raw, list):
            return []
        return raw

    def _write(self, plugins: list[InstalledPlugin]) -> None:
        self._prefs["installed_plugins"] = [p.to_dict() for p in plugins]
        self._save()

    def list(self) -> list[InstalledPlugin]:
        return [InstalledPlugin.from_dict(d) for d in self._raw()]

    def get(self, name: str) -> Optional[InstalledPlugin]:
        for p in self.list():
            if p.name == name:
                return p
        return None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def add(self, entry: InstalledPlugin) -> InstalledPlugin:
        """Add or overwrite an installed-plugin entry (re-install overwrites)."""
        plugins = self.list()
        plugins = [p for p in plugins if p.name != entry.name]
        plugins.append(entry)
        self._write(plugins)
        return entry

    def update(self, name: str, changes: dict[str, Any]) -> Optional[InstalledPlugin]:
        plugins = self.list()
        updated = None
        for p in plugins:
            if p.name == name:
                if "version" in changes:
                    p.version = str(changes["version"])
                if "sha" in changes:
                    p.sha = str(changes["sha"])
                if "description" in changes:
                    p.description = str(changes["description"])
                if "components" in changes and isinstance(changes["components"], dict):
                    p.components = {k: list(v) for k, v in changes["components"].items()}
                if "source_info" in changes and isinstance(changes["source_info"], dict):
                    p.source_info = dict(changes["source_info"])
                updated = p
                break
        if updated is None:
            return None
        self._write(plugins)
        return updated

    def remove(self, name: str) -> bool:
        plugins = self.list()
        target = next((p for p in plugins if p.name == name), None)
        if target is None:
            return False
        self._write([p for p in plugins if p.name != name])
        return True
