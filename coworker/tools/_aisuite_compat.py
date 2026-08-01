"""Compatibility shim for the ``aisuite`` tool-metadata API.

History
-------
``pyproject.toml`` used to pin aisuite to a specific git commit (``1b4bbf3``) on
``andrewyng/aisuite`` that exposed two things the codebase depends on:

* ``aisuite.agents`` — a submodule with a ``@tool`` decorator and a ``ToolMetadata``
  dataclass.
* top-level ``aisuite.ToolMetadata`` / ``aisuite.tool`` — the same symbols re-exported
  on the package root, used by the majority of tool factories via
  ``import aisuite as ai; ai.ToolMetadata(...)``.

That source repo is no longer reachable and the commit was never published to PyPI.
The PyPI release (0.1.14) restructured the package: ``aisuite.agents`` is gone, and
neither ``ToolMetadata`` nor ``tool`` is exported at the top level (only
``Client`` / ``Message`` / ``Tools`` / ``framework`` / ``mcp`` / ``provider`` /
``utils`` remain). As a result every ``ai.ToolMetadata(...)`` and ``ai.tool(...)``
call site — ~15 production modules plus the test suites — raised
``AttributeError: module 'aisuite' has no attribute 'ToolMetadata'``.

What this shim does
-------------------
This module is imported once at process start (via ``coworker.tools.__init__`` and
directly by the three ``coworker.tools.{ask,directories,plan}`` factories). It:

1. Tries the real symbols first (``aisuite.agents`` for the decorator form, top-level
   for the re-export), so a checkout that *does* have the pinned commit still uses
   the upstream implementation unchanged.
2. Falls back to a local ``ToolMetadata`` dataclass + ``tool`` decorator that satisfy
   the contract the runtime relies on (see below).
3. **Installs the fallback symbols onto the ``aisuite`` module itself** when they're
   missing, so every existing ``import aisuite as ai; ai.ToolMetadata(...)`` /
   ``ai.tool(...)`` call site keeps working without editing ~15 files.

Contract
--------
``ToolRegistry.register`` (``registry.py``) reads
``getattr(func, "__aisuite_tool_metadata__", None)`` and stores it opaquely on
``ToolSpec.metadata``. ``risk.classify`` reads ``metadata.requires_approval``.
``manager.py`` reads ``metadata.name`` and ``metadata.requires_approval``. Tests
also assert ``metadata.category`` and ``metadata.capabilities``. So the dataclass
must carry all of: ``name``, ``category``, ``risk_level``, ``capabilities``,
``requires_approval``, ``description`` — all optional, because tests construct it
without ``name``.

``tool(func, metadata=...)`` attaches ``metadata`` to
``func.__aisuite_tool_metadata__`` and returns ``func`` unchanged; the registry
calls ``func`` directly, so no wrapping is needed.

Schema generation (``_schema_for`` in ``registry.py``) uses
``aisuite.utils.tools.Tools``, which *is* present in the PyPI release, so it is
not shimmed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import aisuite as _ai

# --- resolve the canonical symbols -----------------------------------------
# Prefer the real implementations when present (internal fork checkout with the
# pinned commit). Otherwise fall back to the local equivalents below.
try:  # pragma: no cover - depends on which aisuite is installed
    from aisuite.agents import ToolMetadata, tool  # type: ignore[import]
except ImportError:  # pragma: no cover - depends on which aisuite is installed
    @dataclass
    class ToolMetadata:
        """Fallback carrier for tool metadata.

        Mirrors the fields the upstream ``aisuite.agents.ToolMetadata`` exposed.
        All fields are optional: production tool factories pass ``name`` +
        ``requires_approval``; tests often construct one with just ``category`` /
        ``risk_level``. The runtime only reads ``name`` / ``requires_approval``
        (and tests also check ``category`` / ``capabilities``), so a plain
        dataclass is sufficient — ``ToolRegistry`` stores it opaquely on
        ``ToolSpec.metadata``.
        """

        name: str = ""
        category: str = ""
        risk_level: str = "low"
        capabilities: list[str] = field(default_factory=list)
        requires_approval: bool = False
        description: str = ""

    def tool(func: Callable[..., Any], *, metadata: Optional[ToolMetadata] = None) -> Callable[..., Any]:
        """Fallback ``@tool`` decorator: attach metadata, return ``func`` unchanged.

        The upstream decorator wraps the callable; the registry only needs ``__name__``,
        ``__aisuite_tool_metadata__``, and a callable — so returning ``func`` as-is
        satisfies the contract. The metadata attribute is what
        ``ToolRegistry.register`` reads via ``getattr(func, "__aisuite_tool_metadata__", None)``.
        """
        if metadata is not None:
            setattr(func, "__aisuite_tool_metadata__", metadata)
        return func


# --- install onto the aisuite package so `ai.ToolMetadata` / `ai.tool` work --
# The majority of call sites do `import aisuite as ai; ai.ToolMetadata(...)`. Rather
# than edit ~15 production modules + ~10 test files, patch the missing symbols onto
# the imported aisuite module when absent. This is a no-op on a checkout that already
# has them (real or re-exported).
if not hasattr(_ai, "ToolMetadata"):
    _ai.ToolMetadata = ToolMetadata  # type: ignore[attr-defined]
if not hasattr(_ai, "tool"):
    _ai.tool = tool  # type: ignore[attr-defined]


__all__ = ["ToolMetadata", "tool"]
