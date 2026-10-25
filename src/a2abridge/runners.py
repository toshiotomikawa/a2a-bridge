"""Run one turn of a local coding agent in non-interactive mode.

A runner takes the prompt, the session id of an earlier turn (to keep the
conversation), a permission level and a folder, and returns the reply and
the session id to resume next time.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

READ_TOOLS = "Read,Grep,Glob,WebSearch,TodoWrite"
EDIT_TOOLS = READ_TOOLS + ",Edit,Write,NotebookEdit"

# Below "full", Claude Code runs restricted: only the listed built-in
# tools, file tools confined to the working folder, no settings files
# (so no hooks from the folder), no MCP servers, and anything that would
# need a prompt is denied.
_CLAUDE_CONFINED = ["--restricted", "--strict-mcp-config", "--permission-prompts", "none"]

CLAUDE_ARGS = {
    "read": _CLAUDE_CONFINED + ["--permission-mode", "dontAsk",
                                "--tools", READ_TOOLS, "--allowedTools", READ_TOOLS],
    "workspace": _CLAUDE_CONFINED + ["--permission-mode", "acceptEdits", "--tools", EDIT_TOOLS],
    "full": ["--dangerously-skip-permissions"],
}

# Below "full", Codex ignores the user's config.toml: MCP servers and
# plugins run outside its sandbox, so they must not load. The model and
# effort from that file are passed on explicitly.
_CODEX_ISOLATED = ["--ignore-user-config", "--disable", "plugins", "--disable", "apps",
                   "--disable", "computer_use", "-c", 'approval_policy="never"']

CODEX_ARGS = {
    "read": _CODEX_ISOLATED + ["-c", 'sandbox_mode="read-only"'],
    "workspace": _CODEX_ISOLATED + ["-c", 'sandbox_mode="workspace-write"'],
    "full": ["--dangerously-bypass-approvals-and-sandbox"],
}

# Variables that make a nested Claude Code think it runs inside another one.
_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")


class RunError(Exception):
    pass


class Turn:
    """A running subprocess that can be cancelled from another thread."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.stderr = ""
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            if self.proc and self.proc.poll() is None:
                _kill_tree(self.proc)

    def run(self, cmd: list[str], stdin: str, cwd: Path, timeout: float) -> str:
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        with self._lock:
            if self.cancelled:
                raise RunError("cancelled")
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        try:
            out, err = self.proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(self.proc)
            with contextlib.suppress(Exception):
                self.proc.communicate(timeout=10)  # drain pipes, reap the process
            raise RunError(f"timed out after {timeout:.0f}s")
        if self.cancelled:
            raise RunError("cancelled")
        self.stderr = err or ""
        if self.proc.returncode != 0 and not out.strip():
            raise RunError(self.stderr.strip()[-2000:] or f"exit code {self.proc.returncode}")
        return out


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        proc.kill()


def _exe(command: str) -> str:
    found = shutil.which(command)
    if not found:
        raise RunError(f"'{command}' not found on PATH")
    return found


def run_claude(
    turn: Turn, cfg: dict[str, Any], prompt: str, session: str | None, level: str, cwd: Path
) -> tuple[str, str | None]:
    cmd = [_exe(cfg.get("command") or "claude"), "-p", "--output-format", "json"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    if session:
        cmd += ["--resume", session]
    cmd += (cfg.get("args") or {}).get(level) or CLAUDE_ARGS[level]
    out = turn.run(cmd, prompt, cwd, cfg.get("timeout", 1800))
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RunError(
            f"unexpected Claude output: {out[-500:]} {turn.stderr.strip()[-500:]}".strip()
        ) from exc
    if data.get("is_error"):
        raise RunError(str(data.get("result") or data.get("subtype") or "Claude error"))
    return str(data.get("result") or ""), data.get("session_id") or session


def _codex_user_model() -> dict[str, str]:
    """Top-level model and effort from Codex's config.toml, if readable."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    out: dict[str, str] = {}
    try:
        text = (home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if line.startswith("["):
            break  # only top-level keys
        match = re.match(r'^(model|model_reasoning_effort)\s*=\s*"([^"]+)"', line)
        if match:
            out[match.group(1)] = match.group(2)
    return out


def run_codex(
    turn: Turn, cfg: dict[str, Any], prompt: str, session: str | None, level: str, cwd: Path
) -> tuple[str, str | None]:
    fd, last = tempfile.mkstemp(prefix="a2a-codex-", suffix=".txt")
    os.close(fd)
    try:
        cmd = [_exe(cfg.get("command") or "codex"), "exec"]
        if session:
            cmd += ["resume"]
        cmd += ["--json", "--skip-git-repo-check", "-o", last]
        level_args = (cfg.get("args") or {}).get(level) or CODEX_ARGS[level]
        user = _codex_user_model() if "--ignore-user-config" in level_args else {}
        model = cfg.get("model") or user.get("model")
        if model:
            cmd += ["-m", model]
        if user.get("model_reasoning_effort"):
            cmd += ["-c", f'model_reasoning_effort="{user["model_reasoning_effort"]}"']
        cmd += level_args
        if session:
            cmd += [session]
        cmd += ["-"]
        out = turn.run(cmd, prompt, cwd, cfg.get("timeout", 1800))
        thread = session
        errors = []
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread = event.get("thread_id") or thread
            elif event.get("type") in ("error", "turn.failed"):
                errors.append(str(event.get("message") or event.get("error")))
        reply = Path(last).read_text(encoding="utf-8", errors="replace").strip()
        if not reply and errors:
            raise RunError("; ".join(errors))
        return reply, thread
    finally:
        try:
            os.unlink(last)
        except OSError:
            pass


def run_echo(
    turn: Turn, cfg: dict[str, Any], prompt: str, session: str | None, level: str, cwd: Path
) -> tuple[str, str | None]:
    """Test runner: echoes the prompt with the level and turn number."""
    count = int(session or 0) + 1
    return f"echo[{level}#{count}@{cwd.name}]: {prompt.splitlines()[-1]}", str(count)


RUNNERS: dict[str, Callable[..., tuple[str, str | None]]] = {
    "claude": run_claude,
    "codex": run_codex,
    "echo": run_echo,
}
