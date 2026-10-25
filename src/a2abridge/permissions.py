"""Permission levels for inbound requests, and time-limited grants.

Each served agent has a ``default_permission`` (used when a request asks
for nothing) and a ``max_permission``, the ceiling inside the folders
listed in ``workdirs``; anywhere else the ceiling is ``read``. A grant,
made only from this PC's command line, raises the ceiling for a while,
for one folder or for any folder. A request can ask for a level and a
working folder; it gets the lower of what it asked and what that folder
allows, and is told so.

Folders that hold agent or system configuration (``protected``) are
never a working folder below ``full``, and neither is any folder that
contains one, such as the home folder itself.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import config
from .config import PERMISSION_LEVELS

_DIRECTIVE = re.compile(r"^\s*\[(permission|workdir|mode|target)\s*:\s*(.+?)\]\s*$", re.IGNORECASE)

PROTECTED = [
    "~/.a2a",
    "~/.claude",
    "~/.codex",
    "~/.gemini",
    "~/.config",
    "~/.ssh",
    "~/.hermes",
    "~/AppData",
    "~/Documents/PowerShell",
    "~/Documents/WindowsPowerShell",
]


def rank(level: str) -> int:
    return PERMISSION_LEVELS.index(level)


def _grants_file() -> Path:
    return config.state_dir() / "grants.json"


def grants(agent: str | None = None) -> list[dict[str, Any]]:
    live = [g for g in config.read_json(_grants_file(), []) if g["expires"] > time.time()]
    return [g for g in live if agent is None or g["agent"] == agent]


def grant(agent: str, level: str, minutes: float, workdir: str | None = None) -> dict[str, Any]:
    if level not in PERMISSION_LEVELS:
        raise ValueError(f"level must be one of {PERMISSION_LEVELS}")
    entry = {
        "agent": agent,
        "level": level,
        "workdir": str(Path(workdir).expanduser().resolve()) if workdir else None,
        "expires": time.time() + minutes * 60,
    }
    with config.locked(_grants_file()):
        config.write_json(_grants_file(), grants() + [entry])
    return entry


def revoke(agent: str | None = None) -> int:
    with config.locked(_grants_file()):
        before = grants()
        keep = [g for g in before if agent is not None and g["agent"] != agent]
        config.write_json(_grants_file(), keep)
    return len(before) - len(keep)


def parse_directives(text: str) -> tuple[str, dict[str, str]]:
    """Read leading ``[permission: x]``, ``[workdir: y]``, ``[mode: z]`` and
    ``[target: t]`` lines from a message."""
    found: dict[str, str] = {}
    lines = text.splitlines()
    while lines:
        match = _DIRECTIVE.match(lines[0])
        if not match:
            if not lines[0].strip() and found:
                lines.pop(0)
                continue
            break
        found[match.group(1).lower()] = match.group(2).strip()
        lines.pop(0)
    return "\n".join(lines), found


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _inside(path: Path, root: str | Path) -> bool:
    try:
        path.relative_to(_resolve(str(root)))
        return True
    except ValueError:
        return False


def is_protected(folder: Path) -> bool:
    """True if the folder is, is inside, or contains a protected folder."""
    for p in PROTECTED:
        target = _resolve(p)
        if _inside(folder, target) or _inside(target, folder):
            return True
    return False


def _is_remote(path: str) -> bool:
    # UNC paths make Windows try SMB sign-in to the named host on access.
    # Windows reads any mix of two leading slashes as UNC.
    return path.lstrip().replace("/", "\\").startswith("\\\\")


def ceiling(agent: str, agent_cfg: dict[str, Any], folder: Path) -> int:
    """Highest level allowed in this folder, from config and grants."""
    listed = any(_inside(folder, d) for d in agent_cfg.get("workdirs") or [])
    best = rank(agent_cfg.get("max_permission") or "read") if listed else rank("read")
    for g in grants(agent):
        if g["workdir"] is None or _inside(folder, g["workdir"]):
            best = max(best, rank(g["level"]))
    return best


def resolve(
    agent: str,
    agent_cfg: dict[str, Any],
    requested: str | None,
    workdir: str | None,
    current: str | None = None,
) -> tuple[str, Path, list[str]]:
    """Return (level, folder, notes) for one inbound request.

    ``workdir`` is the folder the request asks for; ``current`` is the
    conversation's folder so far, kept when the request asks for a folder
    it may not use.
    """
    notes: list[str] = []
    level = (requested or agent_cfg.get("default_permission") or "read").lower()
    if level not in PERMISSION_LEVELS:
        notes.append(f"unknown permission '{level}', using read")
        level = "read"

    default = _resolve(agent_cfg.get("default_workdir") or "~/.a2a-workspace")
    fallback = _resolve(current) if current else default
    folder = fallback
    if workdir:
        if _is_remote(workdir):
            notes.append(f"network folders are not allowed; using {folder}")
        else:
            asked = _resolve(workdir)
            if not asked.is_dir():
                notes.append(f"folder {asked} does not exist; using {folder}")
            elif is_protected(asked) and min(rank(level), ceiling(agent, agent_cfg, asked)) < rank("full"):
                notes.append(f"folder {asked} holds configuration; using {folder}")
            else:
                folder = asked
    if not folder.is_dir():
        folder.mkdir(parents=True, exist_ok=True)

    allowed = ceiling(agent, agent_cfg, folder)
    if rank(level) > allowed:
        notes.append(
            f"asked for '{level}', allowed up to '{PERMISSION_LEVELS[allowed]}' in {folder}"
        )
        level = PERMISSION_LEVELS[allowed]
    if level != "full" and is_protected(folder):
        notes.append(f"folder {folder} holds configuration; switching to {default}")
        folder = default
        folder.mkdir(parents=True, exist_ok=True)
        level = PERMISSION_LEVELS[min(rank(level), ceiling(agent, agent_cfg, folder))]
    return level, folder, notes
