"""User-facing permission rules — the allow / deny / ask layer.

This is the **user-facing permission layer** (Claude-Code-style permissions), sitting
above the lower-level :mod:`coworker.overrides` risk-class relaxer. The two coexist:

- ``overrides.py`` (``RiskOverrideStore``) — reclassifies a tool's *intrinsic risk*
  (read / write_local / exec / external). Lower-level, drives the permission engine's
  decision tree. Kept as-is.
- ``rules.py`` (``RuleStore``) — the user's explicit permission decision per tool-name
  pattern: ``allow`` (always run, no approval), ``deny`` (block outright), ``ask``
  (force approval even if the tool would otherwise auto-allow). Higher-level, simpler
  mental model, what the Customize ▸ Rules UI exposes.

Resolution: the most specific matching rule wins (same specificity rule as
``overrides._specificity`` — literal chars + exact bonus). ``allow`` maps to risk
``read`` (bypass approval); ``deny`` blocks; ``ask`` forces approval. The agent wires
both resolvers — Rules take precedence as the user's explicit statement of intent.

**Inviolable rule (same as overrides): this store is user-local and is NEVER written by
a persona/package.** Only the user decides tool permissions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Optional


# The three permission actions. ``ask`` is "force approval even for otherwise-read tools"
# — useful for "I want to confirm every time this MCP server deletes anything".
ALLOW = "allow"
DENY = "deny"
ASK = "ask"
ACTIONS = (ALLOW, DENY, ASK)


@dataclass
class Rule:
    """One permission rule. ``pattern`` is a glob matched against the tool name
    (e.g. ``mcp__notion__*``, ``run_shell``, ``write_file``)."""

    pattern: str
    action: str  # one of ACTIONS
    reason: str = ""
    enabled: bool = True
    # Stable id for CRUD (so the UI can update/delete a specific rule even if its
    # pattern changes). Auto-assigned on add.
    id: str = ""


def _specificity(pattern: str) -> int:
    """More literal (non-wildcard) characters = more specific; an exact pattern beats
    any glob. Mirrors ``overrides._specificity`` so both layers rank consistently."""
    literal = sum(1 for c in pattern if c not in "*?[]")
    exact = 0 if any(c in pattern for c in "*?[") else 1000
    return literal + exact


def _new_id() -> str:
    # No Math.random / time in workflow scripts, but this is plain module code —
    # uuid4 is fine and avoids any ordering assumptions.
    import uuid

    return uuid.uuid4().hex[:12]


class RuleStore:
    """Persisted collection of permission rules. Mirrors the
    ``SkillSourceManager.__init__(prefs, save)`` pattern: rules live in the shared
    ``prefs`` dict under the ``"rules"`` key, and mutations call ``save()`` to flush."""

    KEY = "rules"

    def __init__(self, prefs: dict[str, Any], save: Callable[[], None]) -> None:
        self._prefs = prefs
        self._save = save
        # Tolerate a stale/missing key.
        if not isinstance(self._prefs.get(self.KEY), list):
            self._prefs[self.KEY] = []

    # -- internals ----------------------------------------------------------
    def _rules(self) -> list[Rule]:
        out: list[Rule] = []
        for r in self._prefs.get(self.KEY, []):
            try:
                out.append(
                    Rule(
                        pattern=str(r["pattern"]),
                        action=str(r["action"]),
                        reason=str(r.get("reason", "")),
                        enabled=bool(r.get("enabled", True)),
                        id=str(r.get("id", "")),
                    )
                )
            except (KeyError, TypeError):
                continue  # skip malformed
        return out

    def _write(self, rules: list[Rule]) -> None:
        self._prefs[self.KEY] = [
            {
                "id": r.id,
                "pattern": r.pattern,
                "action": r.action,
                "reason": r.reason,
                "enabled": r.enabled,
            }
            for r in rules
        ]
        self._save()

    # -- public API ---------------------------------------------------------
    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        rules = self._rules()
        if enabled_only:
            rules = [r for r in rules if r.enabled]
        return [
            {
                "id": r.id,
                "pattern": r.pattern,
                "action": r.action,
                "reason": r.reason,
                "enabled": r.enabled,
            }
            for r in rules
        ]

    def add(self, pattern: str, action: str, *, reason: str = "", enabled: bool = True) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ValueError(f"invalid action {action!r}; expected one of {ACTIONS}")
        pattern = pattern.strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        rules = self._rules()
        # De-dupe by pattern: replacing an existing rule keeps its id.
        existing = next((r for r in rules if r.pattern == pattern), None)
        rid = existing.id if existing else _new_id()
        rules = [r for r in rules if r.pattern != pattern]
        rule = Rule(pattern=pattern, action=action, reason=reason, enabled=enabled, id=rid)
        rules.append(rule)
        self._write(rules)
        return {
            "id": rule.id,
            "pattern": rule.pattern,
            "action": rule.action,
            "reason": rule.reason,
            "enabled": rule.enabled,
        }

    def update(self, rule_id: str, changes: dict[str, Any]) -> Optional[dict[str, Any]]:
        rules = self._rules()
        for r in rules:
            if r.id == rule_id:
                if "pattern" in changes:
                    p = str(changes["pattern"]).strip()
                    if not p:
                        raise ValueError("pattern must not be empty")
                    r.pattern = p
                if "action" in changes:
                    a = str(changes["action"])
                    if a not in ACTIONS:
                        raise ValueError(f"invalid action {a!r}")
                    r.action = a
                if "reason" in changes:
                    r.reason = str(changes["reason"])
                if "enabled" in changes:
                    r.enabled = bool(changes["enabled"])
                self._write(rules)
                return {
                    "id": r.id,
                    "pattern": r.pattern,
                    "action": r.action,
                    "reason": r.reason,
                    "enabled": r.enabled,
                }
        return None

    def remove(self, rule_id: str) -> bool:
        rules = self._rules()
        before = len(rules)
        rules = [r for r in rules if r.id != rule_id]
        if len(rules) == before:
            return False
        self._write(rules)
        return True

    # -- resolution ---------------------------------------------------------
    def resolve(self, tool_name: str) -> Optional[str]:
        """Return the action (allow/deny/ask) of the most specific *enabled* matching
        rule, or ``None`` if no rule matches (defer to the base risk classification)."""
        best: Optional[str] = None
        best_score = -1
        for r in self._rules():
            if not r.enabled:
                continue
            if fnmatchcase(tool_name, r.pattern):
                score = _specificity(r.pattern)
                if score > best_score:
                    best, best_score = r.action, score
        return best

    def resolver(self) -> Callable[[str], Optional[str]]:
        """A callable for the agent to query rule actions by tool name."""
        return self.resolve
