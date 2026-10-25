"""Configuration and on-disk state, all kept under one home folder.

The home folder is ``$A2A_HOME`` or ``~/.a2a``. It holds ``config.json``
(peers, server settings, tokens) and ``state/`` (conversations, jobs,
grants, contexts). Nothing here is ever part of the repository.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

PERMISSION_LEVELS = ["read", "workspace", "full"]

DEFAULT_CONFIG: dict[str, Any] = {
    "default_peer": "hermes",
    "peers": {
        "hermes": {
            "url": "http://127.0.0.1:9900",
            "token": None,
            "token_env": None,
            "timeout": 900,
            "strip_reasoning": True,
        }
    },
    "server": {
        "host": "127.0.0.1",
        # name -> bearer token. Empty means no auth (localhost only).
        "tokens": {},
        "max_concurrent": 2,
        "dispatch_log": None,
        "agents": {
            "claude": {
                "enabled": True,
                "port": 9901,
                "runner": "claude",
                "name": "Claude Code",
                "command": "claude",
                "model": None,
                "default_permission": "read",
                "max_permission": "workspace",
                "default_workdir": "~",
                "workdirs": [],
                "timeout": 1800,
            },
            "codex": {
                "enabled": True,
                "port": 9902,
                "runner": "codex",
                "name": "Codex",
                "command": "codex",
                "model": None,
                "default_permission": "read",
                "max_permission": "workspace",
                "default_workdir": "~",
                "workdirs": [],
                "timeout": 1800,
            },
        },
    },
}


def home() -> Path:
    return Path(os.environ.get("A2A_HOME") or Path.home() / ".a2a")


def state_dir(*parts: str) -> Path:
    path = home().joinpath("state", *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return home() / "config.json"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    user = json.loads(path.read_text(encoding="utf-8"))
    cfg = _merge(DEFAULT_CONFIG, user)
    # Peers and agents are user-owned maps: keep only what the user lists,
    # once they list any.
    if "peers" in user:
        cfg["peers"] = {
            name: _merge(DEFAULT_CONFIG["peers"]["hermes"], peer)
            for name, peer in user["peers"].items()
        }
    return cfg


def init(peer_name: str = "hermes", peer_url: str | None = None) -> tuple[Path, bool]:
    """Create config.json with a fresh server token. Returns (path, created)."""
    path = config_path()
    if path.exists():
        return path, False
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    peer = cfg["peers"].pop("hermes")
    if peer_url:
        peer["url"] = peer_url
    cfg["peers"] = {peer_name: peer}
    cfg["default_peer"] = peer_name
    cfg["server"]["tokens"] = {peer_name: secrets.token_urlsafe(32)}
    write_json(path, cfg)
    return path, True


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomic write, so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: reader holds the file open
            time.sleep(0.05)
    os.replace(tmp, path)


def peer_token(peer: dict[str, Any]) -> str | None:
    if peer.get("token_env"):
        return os.environ.get(peer["token_env"]) or None
    return peer.get("token") or None
