"""Agent-loop / scheduler-run hooks — run a script before/after a digital human run.

E2 ships the two most useful event points first:

- ``pre_run``  — fired after a ``TaskRun`` is created and registered, before the engine
  is built. Can short-circuit (return non-zero / write ``{"skip": true}`` to stdout) to
  abort the run.
- ``post_run`` — fired in the ``finally`` block of ``_run_scheduled_task``, after
  ``run.status`` is determined (``ok`` / ``error``). The natural extension of the
  existing ``NotifyRouter.dispatch_run`` path.

E2 shipped ``pre_run`` / ``post_run`` (schedule run before/after). This module also
implements the tool-level hooks promised in the original E2 doc:

- ``pre_tool``  — fired before each authorized tool call executes (after permission
  check, before ``registry.execute``). A hook may short-circuit the call by writing
  ``{"skip": true}`` (optionally ``{"result": ...}``) to stdout; the call is then
  answered with that result instead of running. Mirrors Claude Code's PreToolUse.
- ``post_tool`` — fired after the call returns (ok or error), with the result/status.
- ``on_message`` — fired when an assistant message is appended to the history (the
  natural observation point for logging/auditing what the model said).

A Hook = a glob ``match`` (which task/session names trigger it) + a ``command`` (script
path or inline shell) + an ``event``. When the hook fires, a context dict is serialized
to JSON and passed to the command on stdin, so the script can read task name, run id,
status, tool name, arguments, etc. Commands run in a subprocess with a 30s timeout —
hooks must never block the agent loop. Tool events also match on ``match_tool`` (glob
against the tool name; ``"*"`` or absent = all tools) so a hook can target just
``write_file`` without a separate per-tool event type.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Optional

PRE_RUN = "pre_run"
POST_RUN = "post_run"
PRE_TOOL = "pre_tool"
POST_TOOL = "post_tool"
ON_MESSAGE = "on_message"
# Run-level events (schedule before/after) vs tool-level events (per call/message).
RUN_EVENTS = (PRE_RUN, POST_RUN)
TOOL_EVENTS = (PRE_TOOL, POST_TOOL, ON_MESSAGE)
EVENTS = RUN_EVENTS + TOOL_EVENTS

# Hooks are best-effort: a slow or hanging hook must not stall the run. 30s ceiling.
_HOOK_TIMEOUT = 30


@dataclass
class Hook:
    id: str
    name: str
    event: str  # one of EVENTS
    match: str  # glob against the task/session name; "*" = all
    command: str  # shell command or script path
    enabled: bool = True
    # For tool events only: glob against the tool name ("*" or "" = all tools).
    # Ignored for run events (they don't have a tool name to match).
    match_tool: str = "*"


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


class HookStore:
    """Persisted collection of hooks. Same ``__init__(prefs, save)`` pattern as
    :class:`coworker.rules.RuleStore` / :class:`coworker.skills.SkillSourceManager`."""

    KEY = "hooks"

    def __init__(self, prefs: dict[str, Any], save: Callable[[], None]) -> None:
        self._prefs = prefs
        self._save = save
        if not isinstance(self._prefs.get(self.KEY), list):
            self._prefs[self.KEY] = []

    # -- internals ----------------------------------------------------------
    def _hooks(self) -> list[Hook]:
        out: list[Hook] = []
        for h in self._prefs.get(self.KEY, []):
            try:
                out.append(
                    Hook(
                        id=str(h.get("id", "")),
                        name=str(h["name"]),
                        event=str(h["event"]),
                        match=str(h.get("match", "*")),
                        command=str(h["command"]),
                        enabled=bool(h.get("enabled", True)),
                        match_tool=str(h.get("match_tool", "*")),
                    )
                )
            except (KeyError, TypeError):
                continue
        return out

    def _write(self, hooks: list[Hook]) -> None:
        self._prefs[self.KEY] = [
            {
                "id": h.id,
                "name": h.name,
                "event": h.event,
                "match": h.match,
                "command": h.command,
                "enabled": h.enabled,
                "match_tool": h.match_tool,
            }
            for h in hooks
        ]
        self._save()

    # -- CRUD ---------------------------------------------------------------
    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        hooks = self._hooks()
        if enabled_only:
            hooks = [h for h in hooks if h.enabled]
        return [
            {
                "id": h.id,
                "name": h.name,
                "event": h.event,
                "match": h.match,
                "command": h.command,
                "enabled": h.enabled,
                "match_tool": h.match_tool,
            }
            for h in hooks
        ]

    def add(
        self,
        name: str,
        event: str,
        command: str,
        *,
        match: str = "*",
        enabled: bool = True,
        match_tool: str = "*",
    ) -> dict[str, Any]:
        if event not in EVENTS:
            raise ValueError(f"invalid event {event!r}; expected one of {EVENTS}")
        name = name.strip()
        command = command.strip()
        if not name:
            raise ValueError("name must not be empty")
        if not command:
            raise ValueError("command must not be empty")
        # match_tool only applies to tool events; normalize run-event hooks to "*".
        if event in RUN_EVENTS:
            match_tool = "*"
        hooks = self._hooks()
        hook = Hook(
            id=_new_id(),
            name=name,
            event=event,
            match=match or "*",
            command=command,
            enabled=enabled,
            match_tool=match_tool or "*",
        )
        hooks.append(hook)
        self._write(hooks)
        return {
            "id": hook.id,
            "name": hook.name,
            "event": hook.event,
            "match": hook.match,
            "command": hook.command,
            "enabled": hook.enabled,
            "match_tool": hook.match_tool,
        }

    def update(self, hook_id: str, changes: dict[str, Any]) -> Optional[dict[str, Any]]:
        hooks = self._hooks()
        for h in hooks:
            if h.id == hook_id:
                if "name" in changes:
                    n = str(changes["name"]).strip()
                    if not n:
                        raise ValueError("name must not be empty")
                    h.name = n
                if "event" in changes:
                    e = str(changes["event"])
                    if e not in EVENTS:
                        raise ValueError(f"invalid event {e!r}")
                    h.event = e
                    # Switching to a run event drops any tool-specific match.
                    if e in RUN_EVENTS:
                        h.match_tool = "*"
                if "match" in changes:
                    h.match = str(changes["match"]) or "*"
                if "match_tool" in changes and h.event in TOOL_EVENTS:
                    h.match_tool = str(changes["match_tool"]) or "*"
                if "command" in changes:
                    c = str(changes["command"]).strip()
                    if not c:
                        raise ValueError("command must not be empty")
                    h.command = c
                if "enabled" in changes:
                    h.enabled = bool(changes["enabled"])
                self._write(hooks)
                return {
                    "id": h.id,
                    "name": h.name,
                    "event": h.event,
                    "match": h.match,
                    "command": h.command,
                    "enabled": h.enabled,
                    "match_tool": h.match_tool,
                }
        return None

    def remove(self, hook_id: str) -> bool:
        hooks = self._hooks()
        before = len(hooks)
        hooks = [h for h in hooks if h.id != hook_id]
        if len(hooks) == before:
            return False
        self._write(hooks)
        return True

    # -- firing -------------------------------------------------------------
    def fire(self, event: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Fire all enabled hooks matching ``event`` and the context's task name.

        Returns a list of per-hook result dicts (``id``, ``name``, ``ok``, ``stdout``,
        ``stderr``, ``returncode``, ``error``). Never raises — a hook failure is recorded
        in the result, not propagated, so the run is unaffected.

        ``context`` always includes ``event`` and ``task_name`` (best-effort). For tool
        events (``pre_tool`` / ``post_tool`` / ``on_message``) ``context`` also carries
        ``tool_name``; a hook with ``match_tool`` set filters on it (``"*"`` = all).

        Skip semantics (Claude Code PreToolUse model):
        - ``pre_run``  — ``{"skip": true}`` aborts the whole run.
        - ``pre_tool`` — ``{"skip": true}`` skips just this tool call. If ``"result"`` is
          present it becomes the tool's answer (else a generic skip result); recorded as
          ``skip: true`` + ``result`` in the hook result.
        """
        results: list[dict[str, Any]] = []
        task_name = str(context.get("task_name", ""))
        tool_name = str(context.get("tool_name", ""))
        ctx_json = json.dumps(context, ensure_ascii=False)
        for h in self._hooks():
            if not h.enabled or h.event != event:
                continue
            if not fnmatchcase(task_name, h.match):
                continue
            # Tool events also filter on the tool name (glob); run events ignore it.
            if event in TOOL_EVENTS and h.match_tool and h.match_tool != "*":
                if not fnmatchcase(tool_name, h.match_tool):
                    continue
            res: dict[str, Any] = {"id": h.id, "name": h.name, "event": event}
            try:
                proc = subprocess.run(
                    h.command,
                    shell=True,
                    input=ctx_json,
                    capture_output=True,
                    text=True,
                    timeout=_HOOK_TIMEOUT,
                    # Don't inherit the parent's env wholesale, but do pass through
                    # PATH so script paths resolve. Hooks opt into anything else.
                    env={"PATH": os.environ.get("PATH", ""), "OPENWORKER_HOOK_EVENT": event},
                )
                res["ok"] = proc.returncode == 0
                res["returncode"] = proc.returncode
                res["stdout"] = proc.stdout.strip()
                res["stderr"] = proc.stderr.strip()
                # Skip semantics: pre_run aborts the run; pre_tool skips the call.
                if event in (PRE_RUN, PRE_TOOL) and proc.stdout.strip():
                    try:
                        parsed = json.loads(proc.stdout)
                        if isinstance(parsed, dict) and parsed.get("skip"):
                            res["skip"] = True
                            if event == PRE_TOOL and "result" in parsed:
                                res["result"] = parsed["result"]
                    except (json.JSONDecodeError, ValueError):
                        pass
            except subprocess.TimeoutExpired:
                res["ok"] = False
                res["error"] = f"hook timed out after {_HOOK_TIMEOUT}s"
            except Exception as exc:  # never let a hook crash the run
                res["ok"] = False
                res["error"] = str(exc)
            results.append(res)
        return results
