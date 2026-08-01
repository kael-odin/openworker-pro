"""Compatibility shim for ``aisuite.agents`` (``tool`` decorator + ``ToolMetadata``).

The ``pyproject.toml`` pins aisuite to a specific git commit (``1b4bbf3``) that exposes an
``aisuite.agents`` submodule with a ``@tool`` decorator and a ``ToolMetadata`` dataclass. That
commit lives on a fork that was never published to PyPI, and the source repo
(``andrewyng/aisuite``) is no longer reachable. The PyPI release (0.1.14) restructured the
package — ``aisuite.agents`` does not exist there.

This module bridges the two: it tries the real import first (so a checkout that does have the
pinned commit still uses the upstream implementation), and falls back to a local equivalent
otherwise. The fallback is deliberately minimal — it only needs to satisfy the contract the
ToolRegistry in ``registry.py`` relies on:

* ``tool(func, metadata=...)`` attaches ``metadata`` to ``func.__aisuite_tool_metadata__`` and
  returns ``func`` unchanged (the registry reads the attribute and calls ``func`` directly).
* ``ToolMetadata`` is a plain dataclass carrier for ``category`` / ``risk_level`` /
  ``capabilities`` / ``description`` — nothing more, since the registry only stores it as
  opaque ``metadata`` on ``ToolSpec``.

Schema generation (``_schema_for`` in registry.py) uses ``aisuite.utils.tools.Tools``, which *is*
present in the PyPI release, so it is not shimmed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    # The pinned-commit path — present only on the internal fork's checkout.
    from aisuite.agents import ToolMetadata, tool  # type: ignore[import]
except ImportError:  # pragma: no cover - depends on which aisuite is installed
    @dataclass
    class ToolMetadata:
        """Fallback carrier for tool metadata (category/risk/capabilities/description).

        Mirrors the fields the upstream ``aisuite.agents.ToolMetadata`` exposes; the ToolRegistry
        only ever stores it opaquely on ``ToolSpec.metadata``, so a plain dataclass is sufficient.
        """

        category: str = ""
        risk_level: str = "low"
        capabilities: list[str] = field(default_factory=list)
        description: str = ""

    def tool(func: Callable[..., Any], *, metadata: Optional[ToolMetadata] = None) -> Callable[..., Any]:
        """Fallback ``@tool`` decorator: attach metadata, return ``func`` unchanged.

        The upstream decorator wraps the callable; the registry only needs ``__name__``,
        ``__aisuite_tool_metadata__``, and a callable — so returning ``func`` as-is satisfies the
        contract. The metadata attribute is what ``ToolRegistry.register`` reads via
        ``getattr(func, "__aisuite_tool_metadata__", None)``.
        """
        if metadata is not None:
            setattr(func, "__aisuite_tool_metadata__", metadata)
        return func


__all__ = ["ToolMetadata", "tool"]
