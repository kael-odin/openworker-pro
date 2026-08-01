"""Agent coworker platform runtime (codename: coworker)."""

# Install the aisuite ToolMetadata/tool compat shim before any submodule touches
# aisuite. PyPI 0.1.14 dropped both `aisuite.ToolMetadata` and `aisuite.tool`
# (and the `aisuite.agents` submodule); ~15 modules do
# `import aisuite as ai; ai.ToolMetadata(...)`. The shim patches the missing
# symbols onto the aisuite module object, so existing call sites keep working.
# Importing it here (the top-level package) guarantees the patch runs before any
# `coworker.*` submodule body executes — including coworker.web.tool, which
# constructs `ai.ToolMetadata` at import time. See coworker/tools/_aisuite_compat.py.
from .tools import _aisuite_compat  # noqa: F401  (side effect: patches aisuite)

__version__ = "0.0.0"
