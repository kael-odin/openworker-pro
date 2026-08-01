# Install the aisuite ToolMetadata/tool compat shim BEFORE anything else imports
# aisuite — the shim patches the missing symbols onto the aisuite module so that
# `import aisuite as ai; ai.ToolMetadata(...)` works across the codebase under
# PyPI 0.1.14 (which dropped both). Importing it here means any `from .registry
# import ...` (which pulls in aisuite.utils.tools) and every tool factory that
# does `import aisuite as ai` sees the patched module. See _aisuite_compat.py.
from . import _aisuite_compat  # noqa: F401  (side effect: patches aisuite)

from .registry import ToolRegistry, ToolSpec

__all__ = ["ToolRegistry", "ToolSpec"]
