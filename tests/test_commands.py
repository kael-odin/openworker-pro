"""Command loading (slash templates) — 批次 E3.

Mirrors test_skills: CommandLoader discovers <dir>/<name>/COMMAND.md, parses YAML
frontmatter (name/description/allowed-tools) + body (prompt_template), catalog/get, and
command_catalog_text injection.
"""

from __future__ import annotations

from pathlib import Path

from coworker.commands import Command, CommandLoader, command_catalog_text


def _write_command(base: Path, name: str, body: str, *, description: str = "", allowed: str = "") -> None:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    front = "---\n"
    if description:
        front += f"description: {description}\n"
    if allowed:
        front += f"allowed-tools: {allowed}\n"
    front += "---\n\n"
    (d / "COMMAND.md").write_text(front + body, encoding="utf-8")


def test_command_loader_parses_frontmatter_and_body(tmp_path):
    _write_command(
        tmp_path,
        "review",
        "Review the following diff:\n{selection}\n\nCheck for bugs.",
        description="Review a diff for bugs",
        allowed="read_file, grep",
    )
    loader = CommandLoader([tmp_path])
    assert loader.names() == ["review"]
    c = loader.get("review")
    assert isinstance(c, Command)
    assert c.name == "review"
    assert c.description == "Review a diff for bugs"
    assert c.allowed_tools == ["read_file", "grep"]
    assert "Review the following diff:" in c.prompt_template
    assert "{selection}" in c.prompt_template


def test_command_loader_catalog_returns_name_and_description_only(tmp_path):
    _write_command(tmp_path, "a", "body A", description="desc A")
    _write_command(tmp_path, "b", "body B", description="desc B")
    loader = CommandLoader([tmp_path])
    catalog = loader.catalog()
    assert {c["name"] for c in catalog} == {"a", "b"}
    # catalog entries carry only name + description (not the full template).
    for entry in catalog:
        assert set(entry) == {"name", "description"}


def test_command_loader_get_unknown_returns_none(tmp_path):
    _write_command(tmp_path, "x", "body", description="d")
    loader = CommandLoader([tmp_path])
    assert loader.get("x") is not None
    assert loader.get("does-not-exist") is None


def test_command_loader_falls_back_to_dir_name_without_frontmatter(tmp_path):
    # A COMMAND.md with no frontmatter — name comes from the directory.
    d = tmp_path / "plain"
    d.mkdir(parents=True)
    (d / "COMMAND.md").write_text("Just a body, no frontmatter.", encoding="utf-8")
    loader = CommandLoader([tmp_path])
    c = loader.get("plain")
    assert c is not None
    assert c.name == "plain"
    assert c.description == ""  # none declared
    assert c.prompt_template == "Just a body, no frontmatter."
    assert c.allowed_tools == []


def test_command_loader_skips_dirs_without_command_md(tmp_path):
    _write_command(tmp_path, "real", "body", description="d")
    (tmp_path / "not-a-command").mkdir()
    (tmp_path / "not-a-command" / "README.md").write_text("nope", encoding="utf-8")
    loader = CommandLoader([tmp_path])
    assert loader.names() == ["real"]


def test_command_loader_handles_missing_dir_gracefully(tmp_path):
    # A non-existent directory is skipped, not raised.
    loader = CommandLoader([tmp_path / "does-not-exist"])
    assert loader.names() == []
    assert loader.catalog() == []


def test_command_loader_multiple_dirs_aggregate(tmp_path):
    d1 = tmp_path / "user"
    d2 = tmp_path / "builtin"
    _write_command(d1, "mine", "user body", description="user cmd")
    _write_command(d2, "builtin1", "builtin body", description="builtin cmd")
    loader = CommandLoader([d1, d2])
    assert set(loader.names()) == {"mine", "builtin1"}


def test_command_catalog_text_empty_when_no_commands(tmp_path):
    loader = CommandLoader([tmp_path])
    assert command_catalog_text(loader) == ""


def test_command_catalog_text_lists_commands_with_slash_prefix(tmp_path):
    _write_command(tmp_path, "review", "body", description="Review a diff")
    _write_command(tmp_path, "test", "body", description="Run tests")
    loader = CommandLoader([tmp_path])
    text = command_catalog_text(loader)
    assert "/review" in text
    assert "/test" in text
    assert "Review a diff" in text
    assert "Run tests" in text
