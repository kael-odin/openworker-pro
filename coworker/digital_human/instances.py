"""Installed-digital-human instance store.

When a user installs a DHP digital human, an *instance* records: the spec slug, the user's config
values (non-secret), the linked openworker ``task_id``, and install provenance. Secret config values
(field ``is_secret``) are routed to :class:`~coworker.secrets.SecretStore` under a per-instance
profile and never written here.

The store is a single JSON file (``digital-humans.json`` under the state dir) — small, human-readable,
and consistent with how openworker persists personas/automation state. There is no DB because an
instance is metadata, not high-frequency runtime data (the task itself lives in the automation DB).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .spec import DigitalHumanSpec

# SecretStore profile prefix for a digital-human instance's secret config fields.
SECRET_PROFILE_PREFIX = "dh:"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class DigitalHumanInstance:
    """One installed digital human — the spec slug, the user's non-secret config, and the task link."""

    id: str  # instance id (distinct from task_id so re-install / re-link is clean)
    slug: str  # DHP registry slug
    name: str  # spec name at install time (denormalized for listings when the repo is absent)
    task_id: str  # the linked ScheduledTask.id
    config: dict[str, Any] = field(default_factory=dict)  # non-secret user config values
    secret_keys: list[str] = field(default_factory=list)  # which config keys went to SecretStore
    spec_version: str = ""  # spec version at install time (for upgrade detection)
    installed_at: int = 0
    updated_at: int = 0
    # Legacy instances created before the secret-boundary fix may have secret values baked into
    # the task instructions (the preamble used to merge secret values into the JSON). A run that
    # rewrites the instructions (any config/prompt edit via update_digital_human) clears this.
    # Until then, list_dh_instances reports needs_secret_migration so the user is prompted to
    # re-save (which rewrites a clean preamble) rather than silently running a leaky task.
    needs_secret_migration: bool = False
    unreviewed: bool = False

    def secret_profile(self) -> str:
        return f"{SECRET_PROFILE_PREFIX}{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "task_id": self.task_id,
            "config": dict(self.config),
            "secret_keys": list(self.secret_keys),
            "spec_version": self.spec_version,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
            "needs_secret_migration": self.needs_secret_migration,
            "unreviewed": self.unreviewed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DigitalHumanInstance":
        return cls(
            id=str(d.get("id") or ""),
            slug=str(d.get("slug") or ""),
            name=str(d.get("name") or ""),
            task_id=str(d.get("task_id") or ""),
            config=dict(d.get("config") or {}),
            secret_keys=list(d.get("secret_keys") or []),
            spec_version=str(d.get("spec_version") or ""),
            installed_at=int(d.get("installed_at") or 0),
            updated_at=int(d.get("updated_at") or 0),
            # A legacy record (no key) is treated as needing migration: it predates the
            # secret-boundary fix and its instructions may carry plaintext secret values.
            needs_secret_migration=bool(d.get("needs_secret_migration", True)),
            unreviewed=bool(d.get("unreviewed", False)),
        )


class InstanceStore:
    """Persists installed digital-human instances to a JSON file.

    Secret config values live in the SecretStore, not here. This store holds only the non-secret
    config + the task link + provenance. ``resolve_config`` merges the two back together for
    prompt injection (the model sees secret values at run time, never in listings).
    """

    def __init__(self, path: str | Path, secrets=None) -> None:
        self.path = Path(path)
        # Late import to avoid a circular dependency at module load.
        from ..secrets import SecretStore  # noqa: F401
        self.secrets = secrets
        self._instances: dict[str, DigitalHumanInstance] = {}
        self._load()

    # -- persistence ------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in (data.get("instances") or []):
            if not isinstance(item, dict):
                continue
            inst = DigitalHumanInstance.from_dict(item)
            if inst.id:
                self._instances[inst.id] = inst

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"instances": [i.to_dict() for i in self._instances.values()]}
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- queries ----------------------------------------------------------------
    def list(self) -> list[DigitalHumanInstance]:
        return sorted(self._instances.values(), key=lambda i: i.installed_at, reverse=True)

    def get(self, instance_id: str) -> Optional[DigitalHumanInstance]:
        return self._instances.get(instance_id)

    def for_task(self, task_id: str) -> Optional[DigitalHumanInstance]:
        for inst in self._instances.values():
            if inst.task_id == task_id:
                return inst
        return None

    def resolve_config(self, inst: DigitalHumanInstance) -> dict[str, Any]:
        """Merge non-secret config + secret config (resolved from SecretStore) for prompt injection."""
        merged = dict(inst.config)
        if self.secrets is not None and inst.secret_keys:
            secret_data = self.secrets.get(inst.secret_profile()) or {}
            for k in inst.secret_keys:
                if k in secret_data:
                    merged[k] = secret_data[k]
        return merged

    # -- mutations --------------------------------------------------------------
    def put(self, inst: DigitalHumanInstance) -> None:
        if not inst.id:
            inst.id = "dh-" + uuid.uuid4().hex[:10]
        if not inst.installed_at:
            inst.installed_at = _now_ms()
        inst.updated_at = _now_ms()
        self._instances[inst.id] = inst
        self.save()

    def delete(self, instance_id: str) -> bool:
        inst = self._instances.pop(instance_id, None)
        if inst is None:
            return False
        # Best-effort: clear the secret profile too.
        if self.secrets is not None and inst.secret_keys:
            self.secrets.delete(inst.secret_profile())
        self.save()
        return True
