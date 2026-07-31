"""Subagent tools — delegate work to a child TurnEngine with its own context window.

Two flavors share the same pattern (fresh context, `asyncio.run` to completion, only the
final report returns, low-risk metadata so the engine parallelizes independent calls):

1. `explore` — a read-only research subagent over the workspace. Broad questions ("where
   is retry logic handled?") burn the main session's context on dozens of file reads; the
   explorer does them in its own context and returns only the report.

2. `delegate_to_subagent` — generalize the pattern to ANY persona. The main agent delegates
   a subtask to a specialized persona (e.g. an Ops persona to run a checklist, a Code persona
   to refactor a module) which runs in its own context window. Only the report returns.

Both child engines run in plan mode — the PermissionEngine hard-blocks writes/shell no matter
what the child decides — with no approver, so they never need an approval round-trip. That's
what lets them carry low-risk metadata, which in turn makes several delegations in one
assistant turn eligible for the engine's parallel execution. No recursion: each child registry
omits the delegation tool that spawned it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import aisuite as ai

from ..agents.base import Agent, AgentContext
from ..engine import TurnEngine
from ..events import EventType
from ..permissions import Mode, PermissionEngine
from ..tools import ToolRegistry
from .files import file_tools
from .git import git_tools
from .search import search_tools

EXPLORER_INSTRUCTIONS = """You are a read-only code explorer working inside the user's workspace. \
Answer the research task you're given by searching and reading the code (`grep`, `read_file`, \
`list_files`, `git_log`, `git_status`, `git_diff`). You cannot write files or run commands.

Your final message is your report — it goes back to the agent that spawned you, not to the \
user. Make it self-contained: answer the task directly, reference code as path:line, quote the \
key snippets, and note anything surprising you found along the way. If you couldn't find \
something, say what you searched so the caller doesn't repeat the same searches."""

_CHILD_MAX_ITERATIONS = 10

# The delegation tool name, filtered out of every child registry so a subagent can't spawn
# further subagents (no unbounded recursion). Mirrors the explorer's "no explore in child" rule.
_DELEGATE_TOOL_NAME = "delegate_to_subagent"


def build_explorer_engine(
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
    max_iterations: int = _CHILD_MAX_ITERATIONS,
) -> TurnEngine:
    """A child engine with the Code agent's read-only tools and a fresh context."""
    ws = str(Path(workspace).resolve())
    registry = ToolRegistry()
    # Read-only slice of the Code agent's toolset, with the same toolkit replacements
    # (our grep for search_files, our windowed read_file for read_file/read_file_lines).
    replaced = {"search_files", "read_file", "read_file_lines"}
    registry.register_all(
        [
            t
            for t in ai.toolkits.files(root=ws)  # no allow_write → list/read only
            if getattr(t, "__name__", "") not in replaced
        ]
    )
    registry.register_all(file_tools(ws))
    registry.register_all(ai.toolkits.git(root=ws))  # git_status, git_diff
    registry.register_all(git_tools(ws))  # git_log
    registry.register_all(search_tools(ws))  # grep
    permissions = PermissionEngine(workspace_root=Path(ws), mode=Mode.PLAN)
    return TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model=model,
        instructions=EXPLORER_INSTRUCTIONS,
        max_iterations=max_iterations,
        model_settings=model_settings,
    )


def explorer_tools(
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
) -> list:
    def explore(task: str) -> dict:
        """Delegate a broad, read-only research task to a subagent with its own fresh
        context window. It searches and reads the workspace, then returns only its final
        report — the intermediate file reads never touch your context. Use it for
        multi-file questions ("where is X handled?", "how does the Y flow work?"); for a
        single known file, just read it yourself. Independent explore calls run in
        parallel when requested together. State the task precisely and say what the
        report should include.

        Args:
            task (str): The research question, with any constraints and the expected
                shape of the report.
        """
        engine = build_explorer_engine(
            workspace=workspace,
            provider=provider,
            model=model,
            model_settings=model_settings,
        )

        async def _run() -> tuple[str, str]:
            report, status = "", "unknown"
            async for event in engine.run(task):
                if event.type == EventType.ASSISTANT_MESSAGE and event.data.get("text"):
                    report = event.data["text"]
                elif event.type == EventType.TURN_END:
                    status = event.data.get("status", "unknown")
                elif event.type == EventType.ERROR:
                    return report, f"error: {event.data.get('error', '')}"
            return report, status

        # Tools execute in a worker thread (no running loop), so asyncio.run is safe.
        report, status = asyncio.run(_run())
        if not report:
            return {"error": f"explorer produced no report (status: {status})"}
        result: dict[str, Any] = {"report": report}
        if status != "completed":
            result["note"] = (
                f"explorer stopped early ({status}); the report may be partial"
            )
        return result

    return [
        ai.tool(
            explore,
            metadata=ai.ToolMetadata(
                category="search",
                risk_level="low",
                capabilities=["search"],
                requires_approval=False,
            ),
        )
    ]


# -- Generic persona delegation ------------------------------------------------


def build_subagent_engine(
    persona_agent: Agent,
    *,
    workspace: str | Path,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
    max_iterations: int = _CHILD_MAX_ITERATIONS,
) -> TurnEngine:
    """A child engine running a persona's own tools in a fresh context, plan mode.

    Like the explorer: plan mode hard-blocks writes/shell, no approver (so low-risk metadata
    holds and parallel execution is allowed), and the delegation tool is filtered out of the
    child registry to prevent recursion. Unlike the explorer (a fixed read-only toolkit), the
    persona's `tool_factory` decides the toolset — so an Ops persona brings its tools, a Code
    persona brings file/git/search, etc. The persona's system_prompt is reused as the child's
    instructions.
    """
    ws = str(Path(workspace).resolve())
    context = AgentContext(workspace=Path(ws))
    registry = ToolRegistry()
    tools = persona_agent.build_tools(context)
    # No recursion: drop any delegation tool the persona's factory happened to include.
    registry.register_all(
        [t for t in tools if getattr(t, "__name__", "") != _DELEGATE_TOOL_NAME]
    )
    permissions = PermissionEngine(workspace_root=Path(ws), mode=Mode.PLAN)
    instructions = (
        f"{persona_agent.system_prompt}\n\n"
        "You are running as a delegated subagent in your own fresh context window. Do the "
        "task you're given; your final message is your report back to the agent that spawned "
        "you (not the user). Make it self-contained — the caller does not share your context."
    )
    return TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model=model,
        instructions=instructions,
        max_iterations=max_iterations,
        model_settings=model_settings,
    )


def delegate_tools(
    *,
    persona_registry: Any,
    workspace: str | Path,
    provider: Any,
    model: str,
    model_settings: Optional[dict[str, Any]] = None,
) -> list:
    """Expose `delegate_to_subagent` — delegate a subtask to a named persona's own engine.

    `persona_registry` is a PersonaRegistry (or compatible): `.get(persona_id)` returns an
    entry whose `.agent()` yields the runtime Agent. Unknown ids return an error dict.
    """

    def delegate_to_subagent(persona_id: str, task: str) -> dict:
        """Delegate a subtask to a specialized subagent running in its own fresh context
        window. The subagent uses the named persona's tools and prompt, runs to completion,
        and returns only its final report — intermediate tool output never touches your
        context. Use it to fan out specialized work (e.g. one persona audits code while
        another drafts docs); independent delegations run in parallel when requested together.

        Args:
            persona_id (str): The persona to delegate to (e.g. "code", "cowork", or an
                installed persona id). Must be a id from the persona registry.
            task (str): The subtask, with all context the subagent needs (it does NOT share
                your conversation history) and the expected shape of the report.
        """
        entry = persona_registry.get(persona_id)
        if entry is None:
            return {
                "error": f"unknown persona: {persona_id}",
                "available": list(getattr(persona_registry, "ids", lambda: [])()),
            }
        persona_agent = entry.agent()
        engine = build_subagent_engine(
            persona_agent,
            workspace=workspace,
            provider=provider,
            model=model,
            model_settings=model_settings,
        )

        async def _run() -> tuple[str, str]:
            report, status = "", "unknown"
            async for event in engine.run(task):
                if event.type == EventType.ASSISTANT_MESSAGE and event.data.get("text"):
                    report = event.data["text"]
                elif event.type == EventType.TURN_END:
                    status = event.data.get("status", "unknown")
                elif event.type == EventType.ERROR:
                    return report, f"error: {event.data.get('error', '')}"
            return report, status

        # Tools execute in a worker thread (no running loop), so asyncio.run is safe.
        report, status = asyncio.run(_run())
        if not report:
            return {"error": f"subagent produced no report (status: {status})"}
        result: dict[str, Any] = {"report": report}
        if status != "completed":
            result["note"] = (
                f"subagent stopped early ({status}); the report may be partial"
            )
        return result

    return [
        ai.tool(
            delegate_to_subagent,
            metadata=ai.ToolMetadata(
                category="delegation",
                risk_level="low",
                capabilities=["delegate"],
                requires_approval=False,
            ),
        )
    ]
