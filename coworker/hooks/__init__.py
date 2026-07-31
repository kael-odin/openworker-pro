"""Hooks subsystem — agent-loop / scheduler-run hooks (E2).

Currently ships ``pre_run`` / ``post_run`` (digital human schedule run before/after).
Tool-level hooks (``pre_tool`` / ``post_tool`` / ``on_message``) come in a later batch.
"""

from .store import (
    EVENTS,
    POST_RUN,
    PRE_RUN,
    Hook,
    HookStore,
)

__all__ = [
    "EVENTS",
    "PRE_RUN",
    "POST_RUN",
    "Hook",
    "HookStore",
]
