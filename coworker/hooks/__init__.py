"""Hooks subsystem — agent-loop / scheduler-run hooks (E2) + tool-level hooks.

Run-level events (schedule before/after):
  ``pre_run`` / ``post_run`` — fired by the scheduler around a digital human run.

Tool-level events (per call / per assistant message), modeled on Claude Code's
PreToolUse / PostToolUse / UserPrompt hooks:
  ``pre_tool``   — before each authorized tool call executes (may skip the call).
  ``post_tool``  — after the call returns (ok or error), with the result/status.
  ``on_message`` — when an assistant message is appended to the history.

All hooks are best-effort external commands (subprocess, 30s timeout) that receive a
JSON context on stdin. See ``store.HookStore.fire`` for the matching + skip contract.
"""

from .store import (
    EVENTS,
    ON_MESSAGE,
    POST_RUN,
    POST_TOOL,
    PRE_RUN,
    PRE_TOOL,
    RUN_EVENTS,
    TOOL_EVENTS,
    Hook,
    HookStore,
)

__all__ = [
    "EVENTS",
    "RUN_EVENTS",
    "TOOL_EVENTS",
    "PRE_RUN",
    "POST_RUN",
    "PRE_TOOL",
    "POST_TOOL",
    "ON_MESSAGE",
    "Hook",
    "HookStore",
]
