"""Plugin marketplace + install/uninstall (批次 E4).

A plugin is a Claude-Code-format distribution unit: a folder with
``.claude-plugin/plugin.json`` + optional ``skills/`` / ``commands/`` / ``.mcp.json``.
Plugins are installed from marketplace sources (git repos with
``.claude-plugin/marketplace.json``); the official Anthropic marketplace
(``claude-plugins-official``) is built in.

Installed plugins live under ``state_dir()/plugins/<name>/``. The SkillLoader and
CommandLoader scan ``plugins/*/skills/`` and ``plugins/*/commands/`` respectively, so a
plugin's components are picked up by the existing loaders with no loader changes —
the plugin is a *distribution* layer over the existing *execution* layer.
"""

from .sources import PluginSource, PluginSourceManager
from .registry import InstalledPlugin, PluginRegistry
from .installer import (
    PluginInstallError,
    list_catalog,
    install_plugin,
    uninstall_plugin,
    list_installed,
    check_updates,
)

__all__ = [
    "PluginSource",
    "PluginSourceManager",
    "InstalledPlugin",
    "PluginRegistry",
    "PluginInstallError",
    "list_catalog",
    "install_plugin",
    "uninstall_plugin",
    "list_installed",
    "check_updates",
]
