"""PreToolUse hook that confines Grok Build below ``full``.

Grok Build's own permission modes do not stop writes outside the working
folder on Windows, so the bridge runs it with this hook. It reads the
event on stdin and answers deny unless the tool is allowed at the level
in ``A2A_GUARD_LEVEL`` and every path it names is inside
``A2A_GUARD_ROOT``. Any error also denies: Grok runs a crashed hook's
call, so the hook must never crash.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

READ_TOOLS = {"read_file", "grep", "list_dir", "glob", "file_search", "codebase_search", "todo_write"}
WRITE_TOOLS = {"search_replace", "write", "hashline_edit", "edit_file", "delete_file"}
PATH_KEYS = ("path", "file", "dir", "target", "notebook", "glob")
# Never written below "full", even inside the folder: agent and git
# configuration (hooks, plugins, settings) and secrets files.
CONTROL_DIRS = {".git", ".grok", ".claude", ".cursor", ".gemini", ".agents", ".opencode",
                ".codex", ".vscode"}


def _paths(value, key="", tool=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _paths(v, k.lower(), tool)
    elif isinstance(value, list):
        for v in value:
            yield from _paths(v, key, tool)
    elif isinstance(value, str) and any(p in key for p in PATH_KEYS):
        # grep's glob filters file names under the search path; a leading
        # "/" anchors it there. Only a drive, network path or ".." leaves it.
        if tool == "grep" and key == "glob":
            if ":" in value or ".." in value or value.replace("/", "\\").startswith("\\\\"):
                yield value
            return
        yield value


def _resolve(path: str, cwd: Path) -> Path | None:
    if path.lstrip().replace("/", "\\").startswith("\\\\") or path.lstrip().startswith("-"):
        return None
    p = Path(path).expanduser()
    if p.drive and not p.root:  # "C:foo" means a folder of drive C:, not of cwd
        return None
    return (p if p.is_absolute() else cwd / p).resolve()


def _inside(path: str, root: Path, cwd: Path) -> bool:
    p = _resolve(path, cwd)
    if p is None:
        return False
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _is_control(path: str, root: Path, cwd: Path) -> bool:
    p = _resolve(path, cwd)
    if p is None:
        return True
    name = p.name.lower()
    return (any(part.lower() in CONTROL_DIRS for part in p.relative_to(root).parts)
            or name == ".env" or name.startswith(".env.") or name.startswith("opencode.json"))


def decide(event: dict) -> str | None:
    """Return None to allow, or the reason to deny."""
    level = os.environ.get("A2A_GUARD_LEVEL", "")
    root_env = os.environ.get("A2A_GUARD_ROOT", "")
    if level not in ("read", "workspace") or not root_env:
        return "a2a guard is not configured"
    tool = event.get("toolName") or ""
    allowed = READ_TOOLS | (WRITE_TOOLS if level == "workspace" else set())
    if tool not in allowed:
        return f"{tool} is not allowed at the '{level}' level"
    if event.get("toolInputTruncated"):
        return "tool input was truncated"
    root = Path(root_env).resolve()
    cwd = Path(event.get("cwd") or root_env)
    for path in _paths(event.get("toolInput") or {}, tool=tool):
        if not _inside(path, root, cwd):
            return f"{path} is outside the working folder {root}"
        if tool in WRITE_TOOLS and _is_control(path, root, cwd):
            return f"{path} holds configuration or secrets and is not written below 'full'"
    return None


def main() -> None:
    try:
        # Grok Build sends UTF-8; read bytes so the console code page never applies.
        event = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
        reason = decide(event)
        log = os.environ.get("A2A_GUARD_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"tool": event.get("toolName"), "input": event.get("toolInput"),
                                    "deny": reason}) + "\n")
    except Exception as exc:  # noqa: BLE001 - any failure must deny
        reason = f"a2a guard failed: {exc}"
    if reason:
        print(json.dumps({"decision": "deny", "reason": reason}))
        sys.stderr.write(reason + "\n")
        sys.exit(2)
    print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()
