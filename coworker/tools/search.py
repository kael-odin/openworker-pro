"""Fast code search (`grep`) — ripgrep when available, a Python walk otherwise.

ripgrep respects `.gitignore`, so it skips `node_modules`/`target`/`dist` automatically; the
fallback skips a hardcoded set of heavy dirs. Read-only, workspace-scoped. Returns file:line:text.

Parsing note: ripgrep's text output uses ``path:line:text`` with ``:`` as the separator, which
breaks on Windows drive letters (``C:\\path`` — the drive colon is mistaken for the field
separator and the match is dropped). We therefore request ``rg --json`` and parse structured
``type == "match"`` events, which carry path/line/text as discrete JSON fields and are immune
to drive letters, colons in filenames, and Unicode path edge cases.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import aisuite as ai

# Per-OS application data directories. These are not build noise: on macOS 14+ merely
# *descending* into ~/Library/Application Support (other apps' containers) trips the App
# Data TCC protection and macOS shows "would like to access data from other apps" — an
# alarming prompt the user never asked for, reachable whenever the workspace is a home
# directory. Never traversed; a workspace under one of these is still searched normally,
# because the guard matches directory NAMES encountered during a walk.
OS_DATA_DIRS = {
    "Library",  # macOS
    "AppData",  # Windows
    "Application Data",  # Windows (legacy junction)
}

_IGNORE_DIRS = {
    ".git",
    "node_modules",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
} | OS_DATA_DIRS

# OS_DATA_DIRS (AppData, Library, …) must NOT be passed to ripgrep as `--glob !**/<dir>/**`.
# ripgrep's glob matches against the full path, so `!**/AppData/**` would exclude a workspace
# whose path merely *contains* an AppData ancestor (e.g. Windows %TEMP% under
# C:\Users\<user>\AppData\Local\Temp) — silently dropping every match. These dirs only need to
# be skipped during a Python os.walk (which matches by directory NAME at each level, not by
# path). ripgrep already respects .gitignore; the _GLOB_IGNORE set below is the safe subset.
_GLOB_IGNORE = _IGNORE_DIRS - OS_DATA_DIRS

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search the workspace for a regular-expression pattern and return matching lines as "
            "file:line:text. Fast and .gitignore-aware (skips node_modules, build dirs, etc.). "
            "Prefer this over reading files blindly to locate code. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Subdirectory to search (default: whole workspace).",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional filename glob filter, e.g. '*.py'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max matches (default 100, max 1000).",
                },
            },
            "required": ["pattern"],
        },
    },
}


def search_tools(workspace: str) -> list:
    root = Path(workspace).resolve()

    def grep(
        pattern: str,
        path: str = ".",
        glob: Optional[str] = None,
        max_results: int = 100,
    ) -> dict[str, Any]:
        n = max_results if isinstance(max_results, int) and max_results > 0 else 100
        n = min(n, 1000)
        base = (root / (path or ".")).resolve()
        try:
            base.relative_to(root)  # keep searches inside the workspace
        except ValueError:
            return {"error": "path escapes the workspace"}

        rg = shutil.which("rg")
        if rg:
            cmd = [
                rg,
                "--json",
                "--max-count",
                str(n),
                "-e",
                pattern,
            ]
            if glob:
                cmd += ["--glob", glob]
            # Do not rely solely on a workspace's .gitignore: the Python fallback
            # always omits these generated/dependency directories too. Exclusions come
            # last because ripgrep resolves conflicting globs with the later one winning.
            # Use _GLOB_IGNORE (not _IGNORE_DIRS): OS_DATA_DIRS are excluded here only by
            # name during traversal, not as path globs (see _GLOB_IGNORE comment above).
            for ignored in sorted(_GLOB_IGNORE):
                cmd += ["--glob", f"!**/{ignored}/**"]
            cmd.append(str(base))
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except Exception as exc:
                return {"error": f"grep failed: {exc}"}
            if out.returncode not in (0, 1):  # 1 = no matches
                return {"error": (out.stderr or "ripgrep error").strip()[:300]}
            return {"engine": "ripgrep", **_parse_rg_json(out.stdout, root, n)}

        return {"engine": "python", **_py_grep(root, base, pattern, glob, n)}

    grep.__name__ = "grep"
    grep.__doc__ = _SCHEMA["function"]["description"]
    grep.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name="grep",
        category="search",
        risk_level="low",
        capabilities=["search"],
        requires_approval=False,
    )
    grep.__coworker_schema__ = _SCHEMA
    return [grep]


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return path


def _parse_rg_json(stdout: str, root: Path, n: int) -> dict[str, Any]:
    """Parse ripgrep ``--json`` output into structured matches.

    Only ``type == "match"`` events carry search hits; each has ``data.path.text`` (file path),
    ``data.line_number`` and ``data.lines`` (the matching text, as bytes or text). Structured
    JSON avoids the Windows drive-letter / colon-in-path breakage of text-mode parsing.
    """
    matches: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            evt = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            continue  # malformed event — skip, don't abort the whole result
        if evt.get("type") != "match":
            continue
        data = evt.get("data") or {}
        # path.text is the file path (str); fall back to bytes→decode for exotic encodings.
        path_val = (data.get("path") or {}).get("text")
        if not path_val:
            pb = (data.get("path") or {}).get("bytes")
            if pb:
                try:
                    path_val = bytes(pb, "utf-8").decode("utf-8", errors="replace")
                except Exception:
                    continue
        if not path_val:
            continue
        ln = data.get("line_number") or 0
        # lines may be {text: "..."} or {bytes: "..."}; extract the matching line text.
        lines_val = data.get("lines") or {}
        txt = lines_val.get("text")
        if txt is None:
            lb = lines_val.get("bytes")
            if lb:
                try:
                    txt = bytes(lb, "utf-8").decode("utf-8", errors="replace")
                except Exception:
                    txt = ""
        txt = (txt or "").rstrip("\r\n")
        matches.append(
            {
                "file": _rel(str(path_val), root),
                "line": ln,
                "text": txt[:300],
            }
        )
        if len(matches) >= n:
            break
    return {"count": len(matches), "matches": matches}


def _py_grep(
    root: Path, base: Path, pattern: str, glob: Optional[str], n: int
) -> dict[str, Any]:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return {"error": f"invalid regex: {exc}", "count": 0, "matches": []}
    matches: list[dict[str, Any]] = []
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for fn in files:
            if glob and not fnmatch.fnmatch(fn, glob):
                continue
            fp = Path(dirpath) / fn
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            matches.append(
                                {
                                    "file": _rel(str(fp), root),
                                    "line": i,
                                    "text": line.rstrip()[:300],
                                }
                            )
                            if len(matches) >= n:
                                return {"count": len(matches), "matches": matches}
            except OSError:
                continue
    return {"count": len(matches), "matches": matches}
