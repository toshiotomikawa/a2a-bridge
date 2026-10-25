"""Run one turn of a local coding agent in non-interactive mode.

A runner takes the prompt, the session id of an earlier turn (to keep the
conversation), a permission level and a folder, and returns the reply and
the session id to resume next time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

READ_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch,TodoWrite"
EDIT_TOOLS = READ_TOOLS + ",Edit,Write,MultiEdit,NotebookEdit"

CLAUDE_ARGS = {
    "read": ["--permission-mode", "dontAsk", "--allowedTools", READ_TOOLS],
    "workspace": [
        "--permission-mode", "acceptEdits",
        "--allowedTools", EDIT_TOOLS,
        "--disallowedTools", "Bash,PowerShell",
    ],
    "full": ["--dangerously-skip-permissions"],
}

CODEX_ARGS = {
    "read": ["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"'],
    "workspace": ["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"'],
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
            raise RunError(f"timed out after {timeout:.0f}s")
        if self.cancelled:
            raise RunError("cancelled")
        if self.proc.returncode != 0 and not out.strip():
            raise RunError((err or "").strip()[-2000:] or f"exit code {self.proc.returncode}")
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
        raise RunError(f"unexpected Claude output: {out[-500:]}") from exc
    if data.get("is_error"):
        raise RunError(str(data.get("result") or data.get("subtype") or "Claude error"))
    return str(data.get("result") or ""), data.get("session_id") or session


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
        if cfg.get("model"):
            cmd += ["-m", cfg["model"]]
        cmd += (cfg.get("args") or {}).get(level) or CODEX_ARGS[level]
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
