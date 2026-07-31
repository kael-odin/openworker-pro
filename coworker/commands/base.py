"""Command loading — reusable slash commands (`/name` prompt templates).

A command is a folder containing `COMMAND.md` (YAML frontmatter: name, description,
optional allowed-tools) + a markdown body that is the prompt template. Unlike skills,
commands are not agent tools — they are user-triggered: typing `/name` in the Composer
expands the template into the input box (with `{selection}`/`{file}`/`{input}` placeholders
the user fills in), and the expanded text is sent as an ordinary user message.

Progressive disclosure still applies at the catalog level: the agent's instructions get the
list of available command names + descriptions (so it knows which workflows exist), but the
full template is only fetched by the frontend on selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Command:
    name: str
    description: str
    prompt_template: str = ""  # full body — fetched by the frontend on selection
    path: Optional[str] = None
    allowed_tools: list[str] = field(default_factory=list)


class CommandLoader:
    def __init__(self, dirs: list[str | Path]) -> None:
        self._commands: dict[str, Command] = {}
        for directory in dirs:
            self._discover(Path(directory))

    def _discover(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for sub in sorted(directory.iterdir()):
            md = sub / "COMMAND.md"
            if md.is_file():
                command = _parse_command(md)
                self._commands[command.name] = command

    def names(self) -> list[str]:
        return list(self._commands)

    def get(self, name: str) -> Optional[Command]:
        return self._commands.get(name)

    def catalog(self) -> list[dict]:
        return [
            {"name": c.name, "description": c.description}
            for c in self._commands.values()
        ]


def _parse_command(md: Path) -> Command:
    text = md.read_text(encoding="utf-8")
    name, description, allowed, body = md.parent.name, "", [], text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[3:end]
            body = text[end + 4 :].lstrip("\n")
            for line in frontmatter.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key, value = key.strip().lower(), value.strip()
                if key == "name" and value:
                    name = value
                elif key == "description":
                    description = value
                elif key in ("allowed-tools", "allowed_tools"):
                    allowed = [t.strip() for t in value.split(",") if t.strip()]
    return Command(
        name=name,
        description=description,
        prompt_template=body.strip(),
        path=str(md.parent),
        allowed_tools=allowed,
    )


def command_catalog_text(loader: CommandLoader) -> str:
    """Inject the list of available slash commands into the agent's instructions.

    Commands are user-triggered (not agent tools), so this is informational — it lets the
    agent reference available workflows in conversation. The frontend expands `/name` into
    the template before sending; the backend never intercepts the message.
    """
    catalog = loader.catalog()
    if not catalog:
        return ""
    lines = [f"- /{c['name']}: {c['description']}" for c in catalog]
    return (
        "Available slash commands (user-triggered prompt templates — the user types /name "
        "in the Composer to expand one):\n" + "\n".join(lines)
    )
