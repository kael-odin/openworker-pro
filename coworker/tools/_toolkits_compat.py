"""Compatibility shim for ``aisuite.toolkits`` (the ``files()`` / ``git()`` tool factories).

The codebase was built against an aisuite fork commit (``andrewyng/aisuite@1b4bbf3``) that
exposed ``aisuite.toolkits.files()`` / ``aisuite.toolkits.git()`` — factories returning the
file read/write/edit tools and the git status/diff tools. That commit was never published to
PyPI (verified: 0.1.0–0.1.14 all lack the ``toolkits`` submodule), and the source repo is no
longer reachable. PyPI 0.1.14 is a provider-routing-only client with no toolkits at all.

This module reimplements those two factories locally and installs them onto the ``aisuite``
module object (mirroring the ``_aisuite_compat`` metadata-shim approach), so every existing
``ai.toolkits.files(...)`` / ``ai.toolkits.git(...)`` call site — in ``coworker/catalog.py``,
``coworker/tools/subagent.py``, and the test suites — keeps working unchanged.

Contract sources (frozen by tests):
* ``tests/test_catalog.py`` CODE_TOOLS / COWORK_TOOLS — the exact tool-name set.
* ``tests/test_multiroot.py`` — multi-root resolution, runtime root mutation, PermissionError
  on escape / write-to-readonly, ``write_file`` returns the relative path string, ``read_file``
  returns raw text.
* ``coworker/agents/code.py`` prompt — tool signatures (replace_in_file / apply_patch Codex
  format / apply_unified_diff / write_file).
* ``coworker/tools/files.py`` / ``coworker/tools/git.py`` — the per-tool attribute pattern
  (``__name__`` / ``__doc__`` / ``__aisuite_tool_metadata__`` / ``__coworker_schema__``) and
  the subprocess + error-return style.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Union

import aisuite as ai

from ..roots import RootDir, normalize_roots

# Re-export the ToolMetadata/tool the metadata shim installed, so this module doesn't depend
# on import ordering between the two shims.
from ._aisuite_compat import ToolMetadata


# -- path resolution ---------------------------------------------------------

def _roots_list(
    root: Optional[Union[str, Path]] = None,
    roots: Optional[list] = None,
    allow_write: bool = False,
) -> list[RootDir]:
    """Build the shared, mutable roots list.

    ``root`` + ``allow_write`` is the single-root form; ``roots`` is the multi-root form
    (each entry a RootDir / dict / str). When ``roots`` is passed as an already-mutable list
    of ``RootDir`` objects, that very object is returned so runtime ``append``/``remove`` on
    it is seen live by the tools (test_multiroot relies on this). Mixed/non-RootDir entries
    are normalized into a new list (the caller's list isn't mutated).
    """
    if roots is not None:
        # If it's already a list of RootDir, return it by reference (shared, mutable).
        if all(isinstance(r, RootDir) for r in roots):
            return roots
        return normalize_roots(roots)
    if root is not None:
        return [RootDir(path=root, writable=allow_write)]
    # No roots → nothing writable; tools will refuse everything.
    return []


def _resolve(path: str, roots: list[RootDir]) -> tuple[Path, RootDir]:
    """Resolve ``path`` against the roots: relative paths go to the primary (roots[0]);
    absolute paths match whichever root contains them. Raises PermissionError on escape.

    Returns (resolved_target, owning_root).
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        if not roots:
            raise PermissionError("no workspace roots are configured")
        p = roots[0].path / p
    resolved = p.resolve()
    for r in roots:
        try:
            resolved.relative_to(r.path)
            return resolved, r
        except ValueError:
            continue
    raise PermissionError(f"path escapes the workspace roots: {path}")


def _writable_target(path: str, roots: list[RootDir]) -> Path:
    """Resolve a write target, refusing read-only roots and escapes (PermissionError)."""
    resolved, owner = _resolve(path, roots)
    if not owner.writable:
        raise PermissionError(f"path is in a read-only root: {path}")
    return resolved


def _rel(path: Path, roots: list[RootDir]) -> str:
    """Render a path relative to its owning root (or the primary) for return values."""
    for r in roots:
        try:
            return str(path.relative_to(r.path))
        except ValueError:
            continue
    return str(path)


# -- schema helper -----------------------------------------------------------

def _schema(name: str, description: str, params: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params,
                "required": required,
            },
        },
    }


def _stamp(fn: Callable, name: str, category: str, risk_level: str,
           capabilities: list[str], requires_approval: bool, schema: dict) -> Callable:
    fn.__name__ = name
    fn.__doc__ = schema["function"]["description"]
    fn.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=name,
        category=category,
        risk_level=risk_level,
        capabilities=capabilities,
        requires_approval=requires_approval,
    )
    fn.__coworker_schema__ = schema
    return fn


# -- files() factory ---------------------------------------------------------

def files(
    root: Optional[Union[str, Path]] = None,
    roots: Optional[list] = None,
    allow_write: bool = False,
) -> list[Callable]:
    """Local replacement for ``aisuite.toolkits.files()``.

    Returns the eight file tools the catalog/subagent/tests expect: ``list_files``,
    ``write_file``, ``replace_in_file``, ``apply_patch``, ``apply_unified_diff``,
    ``read_file``, ``read_file_lines``, ``search_files``. The catalog filters out
    ``read_file``/``read_file_lines``/``search_files`` and overlays the local enhanced
    versions (numbered ``read_file``, ``grep``); they're included here so the filter has
    something to match and so the raw aisuite-equivalent behavior is available to surfaces
    that don't override (e.g. the explorer's read-only slice).
    """
    rs = _roots_list(root, roots, allow_write)

    # -- list_files ----------------------------------------------------------
    list_files_schema = _schema(
        "list_files",
        "List files in the workspace. Returns relative paths. Set recursive=True to descend "
        "into subdirectories.",
        {
            "path": {"type": "string", "description": "Optional subdirectory to list."},
            "recursive": {"type": "boolean", "description": "Recurse into subdirectories (default False)."},
        },
        [],
    )

    def list_files(path: str = "", recursive: bool = False) -> dict[str, Any]:
        if not rs:
            return {"error": "no workspace roots are configured"}
        if path:
            base = _resolve(path, rs)[0]
        else:
            base = rs[0].path
        if not base.exists() or not base.is_dir():
            return {"error": f"not a directory: {path}"}
        out: list[str] = []
        try:
            if recursive:
                for dirpath, _dirnames, filenames in os.walk(base):
                    for f in sorted(filenames):
                        out.append(str((Path(dirpath) / f).relative_to(rs[0].path)))
            else:
                for entry in sorted(base.iterdir()):
                    out.append(entry.name)
        except OSError as exc:
            return {"error": f"list failed: {exc}"}
        return {"files": out}

    _stamp(list_files, "list_files", "filesystem", "low", ["list"], False, list_files_schema)

    # -- read_file (raw text, aisuite-equivalent — NOT the numbered local version) --
    read_file_schema = _schema(
        "read_file",
        "Read a text file and return its raw contents. Read-only.",
        {"path": {"type": "string", "description": "File path (relative to primary root, or absolute)."}},
        ["path"],
    )

    def read_file(path: str) -> str:
        target = _resolve(path, rs)[0]
        if not target.is_file():
            raise PermissionError(f"not a file: {path}")
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise PermissionError(f"read failed: {exc}")

    _stamp(read_file, "read_file", "filesystem", "low", ["read"], False, read_file_schema)

    # -- read_file_lines -----------------------------------------------------
    read_file_lines_schema = _schema(
        "read_file_lines",
        "Read specific line range of a text file (1-based, inclusive). Returns the selected lines.",
        {
            "path": {"type": "string", "description": "File path."},
            "start_line": {"type": "integer", "description": "First line (1-based, default 1)."},
            "end_line": {"type": "integer", "description": "Last line (1-based, inclusive."},
        },
        ["path"],
    )

    def read_file_lines(path: str, start_line: int = 1, end_line: Optional[int] = None) -> dict[str, Any]:
        target = _resolve(path, rs)[0]
        if not target.is_file():
            return {"error": f"not a file: {path}"}
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return {"error": f"read failed: {exc}"}
        start = start_line if isinstance(start_line, int) and start_line > 0 else 1
        end = end_line if isinstance(end_line, int) and end_line > 0 else len(lines)
        selected = lines[start - 1:end]
        return {
            "path": _rel(target, rs),
            "start_line": start,
            "end_line": start + len(selected) - 1,
            "total_lines": len(lines),
            "lines": selected,
        }

    _stamp(read_file_lines, "read_file_lines", "filesystem", "low", ["read"], False, read_file_lines_schema)

    # -- search_files (name glob — replaced by `grep` in catalog) -----------
    search_files_schema = _schema(
        "search_files",
        "Find files by name glob. Returns matching relative paths.",
        {
            "pattern": {"type": "string", "description": "Filename glob (e.g. '*.py')."},
            "path": {"type": "string", "description": "Optional directory to search in."},
        },
        ["pattern"],
    )

    def search_files(pattern: str = "*", path: str = "") -> dict[str, Any]:
        if not rs:
            return {"error": "no workspace roots are configured"}
        base = _resolve(path, rs)[0] if path else rs[0].path
        if not base.exists():
            return {"error": f"not a directory: {path}"}
        import fnmatch
        out: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(base):
            for f in filenames:
                if fnmatch.fnmatch(f, pattern):
                    out.append(str((Path(dirpath) / f).relative_to(rs[0].path)))
        return {"files": sorted(out)}

    _stamp(search_files, "search_files", "filesystem", "low", ["search"], False, search_files_schema)

    # -- write_file ----------------------------------------------------------
    write_file_schema = _schema(
        "write_file",
        "Create or overwrite a file with the given content. The path must be in a writable root.",
        {
            "path": {"type": "string", "description": "File path (relative to primary root, or absolute)."},
            "content": {"type": "string", "description": "Full file contents."},
        },
        ["path", "content"],
    )

    def write_file(path: str, content: str) -> str:
        target = _writable_target(path, rs)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _rel(target, rs)

    _stamp(write_file, "write_file", "filesystem", "medium", ["write"], True, write_file_schema)

    # -- replace_in_file (Claude Code str_replace semantics) ----------------
    replace_schema = _schema(
        "replace_in_file",
        "Replace a unique exact substring in a file. old_str must match exactly once (unique). "
        "Fails if old_str is not found or matches more than once.",
        {
            "path": {"type": "string", "description": "File path."},
            "old_str": {"type": "string", "description": "Exact text to find (must be unique)."},
            "new_str": {"type": "string", "description": "Replacement text."},
        },
        ["path", "old_str", "new_str"],
    )

    def replace_in_file(path: str, old_str: str, new_str: str) -> dict[str, Any]:
        target = _writable_target(path, rs)
        if not target.is_file():
            return {"error": f"not a file: {path}"}
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_str)
        if count == 0:
            return {"error": "old_str not found in file"}
        if count > 1:
            return {"error": f"old_str matches {count} times; must be unique"}
        target.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        return {"path": _rel(target, rs), "replaced": True}

    _stamp(replace_in_file, "replace_in_file", "filesystem", "medium", ["write"], True, replace_schema)

    # -- apply_patch (Codex format) -----------------------------------------
    apply_patch_schema = _schema(
        "apply_patch",
        "Apply a Codex-style patch. Sections: *** Begin Patch / *** Add File: <path> / "
        "*** Update File: <path> / *** Delete File: <path> / *** End Patch. Inside an Update "
        "section, @@ starts a hunk; lines beginning with a space are context, '-' are removed, "
        "'+' are added.",
        {"patch": {"type": "string", "description": "The full Codex-format patch text."}},
        ["patch"],
    )

    def apply_patch(patch: str) -> dict[str, Any]:
        return _apply_codex_patch(patch, rs)

    _stamp(apply_patch, "apply_patch", "filesystem", "medium", ["write"], True, apply_patch_schema)

    # -- apply_unified_diff --------------------------------------------------
    unified_schema = _schema(
        "apply_unified_diff",
        "Apply a standard unified diff. Each hunk's @@ header gives the file path via the "
        "---/+++ lines preceding it.",
        {"diff": {"type": "string", "description": "Standard unified diff text."}},
        ["diff"],
    )

    def apply_unified_diff(diff: str) -> dict[str, Any]:
        return _apply_unified_diff(diff, rs)

    _stamp(apply_unified_diff, "apply_unified_diff", "filesystem", "medium", ["write"], True, unified_schema)

    # Read tools are always present; write tools only when at least one root is writable
    # (mirrors the original aisuite behavior: ``files(root=, allow_write=False)`` returns a
    # read-only slice with no write_file/replace_in_file/apply_patch/apply_unified_diff —
    # test_subagent's explorer asserts they're absent).
    tools: list[Callable] = [list_files, read_file, read_file_lines, search_files]
    if any(r.writable for r in rs):
        tools += [write_file, replace_in_file, apply_patch, apply_unified_diff]
    return tools


# -- Codex patch parser ------------------------------------------------------

def _apply_codex_patch(patch: str, roots: list[RootDir]) -> dict[str, Any]:
    """Apply a Codex-style patch. See coworker/agents/code.py prompt for the format."""
    lines = patch.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        return {"error": "patch must start with '*** Begin Patch'"}
    if lines[-1].strip() != "*** End Patch":
        return {"error": "patch must end with '*** End Patch'"}

    applied: list[str] = []
    i = 1
    n = len(lines) - 1  # exclude the trailing *** End Patch
    while i < n:
        header = lines[i].strip()
        if header.startswith("*** Add File: "):
            rel = header[len("*** Add File: "):]
            i += 1
            content_lines: list[str] = []
            while i < n and not lines[i].startswith("*** "):
                line = lines[i]
                if line.startswith("+"):
                    content_lines.append(line[1:])
                elif line.startswith(" "):
                    content_lines.append(line[1:])
                else:
                    return {"error": f"unexpected line in Add File: {line!r}"}
                i += 1
            target = _writable_target(rel, roots)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(content_lines) + ("\n" if content_lines else ""), encoding="utf-8")
            applied.append(f"added {rel}")
        elif header.startswith("*** Delete File: "):
            rel = header[len("*** Delete File: "):]
            target = _writable_target(rel, roots)
            if target.is_file():
                target.unlink()
                applied.append(f"deleted {rel}")
            else:
                return {"error": f"delete target not found: {rel}"}
            i += 1
        elif header.startswith("*** Update File: "):
            rel = header[len("*** Update File: "):]
            i += 1
            target = _writable_target(rel, roots)
            if not target.is_file():
                return {"error": f"update target not found: {rel}"}
            text = target.read_text(encoding="utf-8", errors="replace")
            current = text.splitlines(keepends=True)
            result: list[str] = []
            ci = 0  # cursor into current
            while i < n and not lines[i].startswith("*** "):
                hunk_marker = lines[i]
                if not hunk_marker.startswith("@@"):
                    # context outside a hunk — treat as a single implicit hunk
                    hunk_lines = []
                    start = i
                    while i < n and not lines[i].startswith("*** ") and not lines[i].startswith("@@"):
                        hunk_lines.append(lines[i])
                        i += 1
                    consumed, err = _apply_hunk(current, ci, hunk_lines, result)
                    if err:
                        return {"error": err}
                    ci = consumed
                    continue
                i += 1  # consume the @@ line
                hunk_lines: list[str] = []
                while i < n and not lines[i].startswith("*** ") and not lines[i].startswith("@@"):
                    hunk_lines.append(lines[i])
                    i += 1
                consumed, err = _apply_hunk(current, ci, hunk_lines, result)
                if err:
                    return {"error": err}
                ci = consumed
            # append any trailing unchanged tail
            result.extend(current[ci:])
            target.write_text("".join(result), encoding="utf-8")
            applied.append(f"updated {rel}")
        else:
            return {"error": f"unknown patch section: {header!r}"}

    return {"applied": applied}


def _apply_hunk(current: list[str], ci: int, hunk_lines: list[str], result: list[str]) -> tuple[int, Optional[str]]:
    """Apply one Codex hunk. Context lines (leading space) must match current; '-' removed;
    '+' added. Returns (new_cursor, error_or_None). Advances `ci` past matched context + removed.
    """
    # First, find the anchor: leading context lines locate where in `current` we are.
    # We copy context + additions into `result` and skip removals.
    idx = ci
    # Locate the start by matching the first context line (if any) from ci forward.
    lead_context = [hl for hl in hunk_lines if hl.startswith(" ")]
    if lead_context:
        anchor = lead_context[0][1:]
        # search forward from idx for the anchor line content
        found = -1
        for j in range(idx, len(current)):
            if current[j].rstrip("\n") == anchor:
                found = j
                break
        if found < 0:
            return ci, f"context not found: {anchor!r}"
        # copy untouched lines up to the anchor
        result.extend(current[idx:found])
        idx = found

    for hl in hunk_lines:
        if not hl:
            continue
        tag, rest = hl[0], hl[1:]
        if tag == " ":
            if idx >= len(current) or current[idx].rstrip("\n") != rest:
                return ci, f"context mismatch: {rest!r}"
            result.append(current[idx])
            idx += 1
        elif tag == "-":
            if idx >= len(current) or current[idx].rstrip("\n") != rest:
                return ci, f"remove mismatch: {rest!r}"
            idx += 1
        elif tag == "+":
            result.append(rest + "\n")
        else:
            return ci, f"bad hunk line: {hl!r}"
    return idx, None


# -- unified diff parser -----------------------------------------------------

def _apply_unified_diff(diff: str, roots: list[RootDir]) -> dict[str, Any]:
    """Apply a standard unified diff. Parses ---/+++ file headers and @@ hunks."""
    lines = diff.splitlines()
    applied: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("--- "):
            old_path = line[4:].split("\t")[0].strip()
            if old_path == "/dev/null":
                old_path = None
            i += 1
            if i >= n or not lines[i].startswith("+++ "):
                return {"error": "+++ header missing after ---"}
            new_path = lines[i][4:].split("\t")[0].strip()
            i += 1
            if new_path == "/dev/null":
                # deletion
                if old_path is None:
                    return {"error": "deletion diff with no source path"}
                target = _writable_target(old_path, roots)
                if target.is_file():
                    target.unlink()
                    applied.append(f"deleted {old_path}")
                i += 1
                continue
            # gather hunks for this file
            hunks: list[list[str]] = []
            while i < n and lines[i].startswith("@@"):
                i += 1
                hunk: list[str] = []
                while i < n and not lines[i].startswith("@@") and not lines[i].startswith("--- "):
                    hunk.append(lines[i])
                    i += 1
                hunks.append(hunk)
            target = _writable_target(new_path, roots)
            if old_path is None:
                # new file: hunk additions become content
                content_lines = [hl[1:] for hunk in hunks for hl in hunk if hl.startswith("+")]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(content_lines) + ("\n" if content_lines else ""), encoding="utf-8")
                applied.append(f"added {new_path}")
            else:
                if not target.is_file():
                    return {"error": f"diff target not found: {new_path}"}
                text = target.read_text(encoding="utf-8", errors="replace")
                current = text.splitlines(keepends=True)
                result: list[str] = []
                ci = 0
                for hunk in hunks:
                    consumed, err = _apply_hunk(current, ci, hunk, result)
                    if err:
                        return {"error": err}
                    ci = consumed
                result.extend(current[ci:])
                target.write_text("".join(result), encoding="utf-8")
                applied.append(f"updated {new_path}")
        else:
            i += 1
    return {"applied": applied}


# -- git() factory -----------------------------------------------------------

def git(
    root: Optional[Union[str, Path]] = None,
    roots: Optional[list] = None,
) -> list[Callable]:
    """Local replacement for ``aisuite.toolkits.git()`` — returns ``git_status`` + ``git_diff``.

    Mirrors the subprocess + error-return style of ``coworker/tools/git.py``.
    """
    rs = _roots_list(root, roots, allow_write=False)
    ws = str(rs[0].path) if rs else str(Path(root or ".").resolve())

    git_status_schema = _schema(
        "git_status",
        "Show working-tree status (porcelain). Read-only.",
        {"path": {"type": "string", "description": "Optional pathspec to limit scope."}},
        [],
    )

    def git_status(path: str = "") -> dict[str, Any]:
        cmd = ["git", "-C", ws, "status", "--porcelain=v1"]
        if path:
            cmd += ["--", path]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except Exception as exc:
            return {"error": f"git status failed: {exc}"}
        if out.returncode != 0:
            return {"error": (out.stderr or "git status failed").strip()[:300]}
        entries = []
        for line in out.stdout.splitlines():
            if not line:
                continue
            entries.append({"xy": line[:2], "path": line[3:]})
        return {"entries": entries}

    _stamp(git_status, "git_status", "git", "low", ["git"], False, git_status_schema)

    git_diff_schema = _schema(
        "git_diff",
        "Show unstaged changes as a unified diff. Read-only.",
        {"path": {"type": "string", "description": "Optional pathspec to limit scope."}},
        [],
    )

    def git_diff(path: str = "") -> dict[str, Any]:
        cmd = ["git", "-C", ws, "diff"]
        if path:
            cmd += ["--", path]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except Exception as exc:
            return {"error": f"git diff failed: {exc}"}
        if out.returncode != 0:
            return {"error": (out.stderr or "git diff failed").strip()[:300]}
        return {"diff": out.stdout}

    _stamp(git_diff, "git_diff", "git", "low", ["git"], False, git_diff_schema)

    return [git_status, git_diff]


# -- install onto the aisuite module -----------------------------------------

def _install() -> None:
    """Patch ``aisuite.toolkits`` with the local ``files`` / ``git`` when absent."""
    if not hasattr(ai, "toolkits"):
        ai.toolkits = SimpleNamespace(files=files, git=git)  # type: ignore[attr-defined]


# Install on import (this module is imported from coworker/__init__.py before any
# ai.toolkits.* call site runs).
_install()


__all__ = ["files", "git"]
