"""Permission levels for inbound requests, and time-limited grants.

Each served agent has a ``default_permission`` (used when a request asks
for nothing) and a ``max_permission`` (the ceiling). A grant, made only
from this PC's command line, raises the ceiling for a while, optionally
for one folder. A request can ask for a level and a working folder; it
gets the lower of what it asked and what is allowed, and is told so.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import config
from .config import PERMISSION_LEVELS

_DIRECTIVE = re.compile(r"^\s*\[(permission|workdir)\s*:\s*(.+?)\]\s*$", re.IGNORECASE)


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
    config.write_json(_grants_file(), grants() + [entry])
    return entry


def revoke(agent: str | None = None) -> int:
    before = grants()
    keep = [g for g in before if agent is not None and g["agent"] != agent]
    config.write_json(_grants_file(), keep)
    return len(before) - len(keep)


def parse_directives(text: str) -> tuple[str, dict[str, str]]:
    """Read leading ``[permission: x]`` / ``[workdir: y]`` lines from a message."""
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


def _inside(path: Path, root: str) -> bool:
    try:
        path.relative_to(Path(root).expanduser().resolve())
        return True
    except ValueError:
        return False


def resolve(
    agent: str, agent_cfg: dict[str, Any], requested: str | None, workdir: str | None
) -> tuple[str, Path, list[str]]:
    """Return (level, workdir, notes) for one inbound request."""
    notes: list[str] = []
    level = (requested or agent_cfg.get("default_permission") or "read").lower()
    if level not in PERMISSION_LEVELS:
        notes.append(f"unknown permission '{level}', using read")
        level = "read"

    folder = Path(workdir or agent_cfg.get("default_workdir") or "~").expanduser().resolve()
    if not folder.is_dir():
        notes.append(f"folder {folder} does not exist, using the default")
        folder = Path(agent_cfg.get("default_workdir") or "~").expanduser().resolve()

    allowed = rank(agent_cfg.get("max_permission") or "read")
    allowed_dirs = list(agent_cfg.get("workdirs") or [])
    for g in grants(agent):
        if g["workdir"] is None or _inside(folder, g["workdir"]):
            allowed = max(allowed, rank(g["level"]))
            if g["workdir"]:
                allowed_dirs.append(g["workdir"])

    if rank(level) > allowed:
        notes.append(f"asked for '{level}', allowed up to '{PERMISSION_LEVELS[allowed]}'")
        level = PERMISSION_LEVELS[allowed]

    # Writing needs a folder the owner listed or granted; reading does not.
    if level != "read" and not any(_inside(folder, d) for d in allowed_dirs):
        has_global_grant = any(g["workdir"] is None and rank(g["level"]) >= rank(level)
                               for g in grants(agent))
        if not has_global_grant:
            notes.append(f"folder {folder} is not allowed for '{level}', using read")
            level = "read"
    return level, folder, notes
