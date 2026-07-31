"""delegate_to_subagent — generic persona delegation (批次 E3).

Generalizes the explore pattern: build a child engine from any persona's Agent, run it in
plan mode with no approver (low-risk → parallel-safe), filter the delegation tool out of
the child (no recursion), return only the report.
"""

from __future__ import annotations

from pathlib import Path

from coworker.agents.base import Agent, AgentContext
from coworker.permissions import Mode
from coworker.providers import (
    AssistantTurn,
    ModelCapabilities,
    ProviderClient,
    ToolCall,
)
from coworker.tools import ToolRegistry
from coworker.tools.subagent import build_subagent_engine, delegate_tools


def _text_turn(text):
    return AssistantTurn(text=text, finish_reason="stop")


def _tool_turn(name, args, call_id="call_1"):
    return AssistantTurn(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
    )


class ScriptedProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


class _Entry:
    """Minimal stand-in for PersonaEntry — exposes .agent()."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def agent(self) -> Agent:
        return self._agent


class _StubRegistry:
    """Minimal stand-in for PersonaRegistry — .get(id) → _Entry or None, .ids() → list."""

    def __init__(self, entries: dict[str, _Entry]) -> None:
        self._entries = entries

    def get(self, persona_id: str):
        return self._entries.get(persona_id)

    def ids(self):
        return list(self._entries)


def _persona_agent(*, name="ops", system_prompt="You are an Ops persona.") -> Agent:
    """A persona whose tool_factory exposes one trivial tool (so the child registry isn't empty)."""

    def factory(ctx: AgentContext) -> list:
        def greet() -> dict:
            """Return a fixed greeting."""
            return {"hello": "from " + name}

        import aisuite as ai

        return [
            ai.tool(
                greet,
                metadata=ai.ToolMetadata(
                    category="test", risk_level="low", capabilities=["greet"]
                ),
            )
        ]

    return Agent(
        name=name,
        title=name,
        system_prompt=system_prompt,
        needs_workspace=True,
        tool_factory=factory,
        family="code",
    )


def test_build_subagent_engine_uses_persona_tools_and_plan_mode(tmp_path):
    agent = _persona_agent()
    engine = build_subagent_engine(
        agent, workspace=tmp_path, provider=ScriptedProvider([]), model="gpt-5.5"
    )
    # The persona's greet tool is present.
    assert "greet" in engine.registry.names()
    # No recursion: delegate_to_subagent is filtered out.
    assert "delegate_to_subagent" not in engine.registry.names()
    # Plan mode hard-blocks writes/shell.
    assert engine.permissions.mode is Mode.PLAN


def test_delegate_to_subagent_returns_report(tmp_path):
    agent = _persona_agent()
    registry = _StubRegistry({"ops": _Entry(agent)})
    provider = ScriptedProvider(
        [
            _tool_turn("greet", {}),
            _text_turn("Ops check complete: 1 greeting collected."),
        ]
    )
    reg = ToolRegistry()
    reg.register_all(
        delegate_tools(
            persona_registry=registry,
            workspace=tmp_path,
            provider=provider,
            model="gpt-5.5",
        )
    )
    spec = reg.get("delegate_to_subagent")
    assert spec.metadata.risk_level == "low"  # parallel-safe
    assert spec.metadata.requires_approval is False

    result = reg.execute(
        "delegate_to_subagent",
        {"persona_id": "ops", "task": "run the ops checklist"},
    )
    assert result["report"] == "Ops check complete: 1 greeting collected."
    assert "note" not in result  # completed normally


def test_delegate_to_subagent_unknown_persona_returns_error(tmp_path):
    registry = _StubRegistry({})  # no personas
    reg = ToolRegistry()
    reg.register_all(
        delegate_tools(
            persona_registry=registry,
            workspace=tmp_path,
            provider=ScriptedProvider([]),
            model="gpt-5.5",
        )
    )
    result = reg.execute(
        "delegate_to_subagent",
        {"persona_id": "nonexistent", "task": "anything"},
    )
    assert "error" in result
    assert "nonexistent" in result["error"]
    assert "available" in result  # lists what's installed


def test_delegate_to_subagent_child_has_no_delegate_tool(tmp_path):
    # A persona whose factory ALSO tries to expose delegate_to_subagent — the child must
    # still not have it (filtered to prevent recursion).
    def factory(ctx: AgentContext) -> list:
        def delegate_to_subagent(persona_id: str, task: str) -> dict:  # noqa: ARG001
            """Should never exist in the child."""
            return {}

        import aisuite as ai

        return [
            ai.tool(
                delegate_to_subagent,
                metadata=ai.ToolMetadata(
                    category="delegation", risk_level="low", capabilities=["delegate"]
                ),
            )
        ]

    agent = Agent(
        name="sneaky",
        title="sneaky",
        system_prompt="x",
        needs_workspace=True,
        tool_factory=factory,
        family="code",
    )
    engine = build_subagent_engine(
        agent, workspace=tmp_path, provider=ScriptedProvider([]), model="gpt-5.5"
    )
    assert "delegate_to_subagent" not in engine.registry.names()


def test_delegate_to_subagent_flags_partial_on_iteration_rail(tmp_path):
    agent = _persona_agent()

    class LoopingProvider(ScriptedProvider):
        def complete(self, **kwargs):
            return _tool_turn("greet", {})

    registry = _StubRegistry({"ops": _Entry(agent)})
    reg = ToolRegistry()
    reg.register_all(
        delegate_tools(
            persona_registry=registry,
            workspace=tmp_path,
            provider=LoopingProvider([]),
            model="gpt-5.5",
        )
    )
    result = reg.execute(
        "delegate_to_subagent", {"persona_id": "ops", "task": "loop"}
    )
    # Hit max_iterations → either an error or a partial-report note.
    assert "max_iterations" in result.get("error", "") or "max_iterations" in result.get(
        "note", ""
    )
