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
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from . import config

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

    def run(
        self, cmd: list[str], stdin: str, cwd: Path, timeout: float,
        env: dict[str, str] | None = None,
    ) -> str:
        base = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env = {**base, **(env or {})}
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


# Windows caps a command line near 32,767 characters; runners that pass
# the prompt as an argument refuse longer ones.
_MAX_ARG = 30000


def _arg_prompt(prompt: str) -> str:
    if len(prompt) > _MAX_ARG:
        raise RunError(f"message too long for this agent ({len(prompt)} characters, at most {_MAX_ARG})")
    return prompt


def _real_exe(command: str) -> str:
    """Like _exe, but never a .cmd or .bat shim: cmd.exe would reparse the prompt."""
    found = _exe(command)
    if not found.lower().endswith((".cmd", ".bat")):
        return found
    if os.name == "nt" and not Path(command).suffix:
        exe = shutil.which(command + ".exe")
        if exe:
            return exe
    # Where the installers put the real binaries their shims call.
    known = {
        "opencode": Path(found).parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe",
        "grok": Path.home() / ".grok" / "bin" / "grok.exe",
    }.get(Path(command).stem.lower())
    if known and known.exists():
        return str(known)
    raise RunError(f"'{found}' is a batch shim; set 'command' to the real executable")


def _level_session(session: str | None, level: str) -> str | None:
    """Sessions saved as 'level:id'; a level change starts a new one."""
    if session and session.startswith(level + ":"):
        return session.split(":", 1)[1]
    return None


def _isolated(name: str) -> Path:
    path = config.home() / "isolated" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_if_changed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")


def _json_tail(out: str, what: str, turn: Turn) -> dict[str, Any]:
    try:
        return json.loads(out[out.index("{"):])
    except ValueError as exc:
        raise RunError(
            f"unexpected {what} output: {out[-500:]} {turn.stderr.strip()[-500:]}".strip()
        ) from exc


# Antigravity (agy) has no flag to confine it, and the user's settings may
# allow every command. Below "full" it runs with its home folder pointed at
# an isolated one (sign-in lives in the system keyring, so it still works),
# which loads no plugins, MCP servers or user settings, and whose settings
# keep it in the working folder and deny commands, MCP and URLs.
_AGY_DENY = ["command(*)", "mcp(*)", "execute_url(*)"]
AGY_SETTINGS = {
    "read": {"toolPermission": "request-review", "allowNonWorkspaceAccess": False,
             "permissions": {"allow": [], "deny": ["write_file(*)"] + _AGY_DENY}},
    "workspace": {"toolPermission": "request-review", "allowNonWorkspaceAccess": False,
                  "artifactReviewPolicy": "always-proceed",
                  "permissions": {"allow": [], "deny": _AGY_DENY}},
}
AGY_ARGS = {"read": [], "workspace": ["--mode", "accept-edits"],
            "full": ["--dangerously-skip-permissions"]}


def run_agy(
    turn: Turn, cfg: dict[str, Any], prompt: str, session: str | None, level: str, cwd: Path
) -> tuple[str, str | None]:
    resume = _level_session(session, level)
    cmd = [_real_exe(cfg.get("command") or "agy"), "-p", _arg_prompt(prompt),
           "--output-format", "json"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    if resume:
        cmd += ["--conversation", resume]
    cmd += (cfg.get("args") or {}).get(level) or AGY_ARGS[level]
    env = None
    if level != "full":
        home = _isolated(f"agy-{level}")
        _write_if_changed(home / ".gemini" / "antigravity-cli" / "settings.json",
                          json.dumps(AGY_SETTINGS[level], indent=2))
        env = {"USERPROFILE": str(home)}
    data = _json_tail(turn.run(cmd, "", cwd, cfg.get("timeout", 1800), env), "agy", turn)
    reply = str(data.get("response") or "").strip()
    denied = data.get("denied_actions") or []
    if denied:
        refused = "; ".join(json.dumps(d, ensure_ascii=False)[:200] for d in denied)
        if reply:
            reply += f"\n\n[a2a-bridge: refused at this level: {refused}]"
        else:
            # agy ends the turn at a refused action, often before it replies.
            reply = f"[a2a-bridge: agy stopped when this level refused: {refused}. Ask again without that step.]"
    if not reply:
        raise RunError(f"agy returned no reply (status {data.get('status')})")
    new = data.get("conversation_id")
    return reply, f"{level}:{new}" if new else session


# OpenCode takes an inline config that outranks the user's: below "full"
# every tool is denied except reading (and, for "workspace", editing)
# inside the working folder; its own config files stay off limits. The
# folder's own OpenCode config, agents and plugins (plugins run as code)
# and Claude Code compatibility files are not loaded.
_ENV_FILES = {"*.env": "deny", "*.env.*": "deny"}
_OC_BASE = {"*": "deny", "read": {"*": "allow", **_ENV_FILES},
            "glob": "allow", "grep": "allow", "list": "allow", "todowrite": "allow",
            "external_directory": "deny", "task": "deny", "question": "deny"}
OPENCODE_PERMISSION = {
    "read": _OC_BASE,
    "workspace": {**_OC_BASE, "edit": {"*": "allow", **_ENV_FILES, "*opencode.json*": "deny",
                                       "*.opencode*": "deny"}},
}
_OC_ISOLATED = {"OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1"}


def run_opencode(
    turn: Turn, cfg: dict[str, Any], prompt: str, session: str | None, level: str, cwd: Path
) -> tuple[str, str | None]:
    # A session keeps permissions approved in it, so one level never
    # resumes another level's session.
    resume = _level_session(session, level)
    cmd = [_real_exe(cfg.get("command") or "opencode"), "run", "--standalone", "--format", "json"]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    if resume:
        cmd += ["--session", resume]
    env = None
    if level == "full":
        cmd += (cfg.get("args") or {}).get(level) or ["--auto"]
    else:
        cmd += (cfg.get("args") or {}).get(level) or []
        env = {**_OC_ISOLATED,
               "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": OPENCODE_PERMISSION[level]})}
    cmd += [_arg_prompt(prompt)]
    out = turn.run(cmd, "", cwd, cfg.get("timeout", 1800), env)
    texts, errors, sid = [], [], resume
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = event.get("sessionID") or sid
        if event.get("type") == "text":
            texts.append(str((event.get("part") or {}).get("text") or ""))
        elif event.get("type") == "error":
            errors.append(json.dumps(event.get("error"), ensure_ascii=False)[:500])
    reply = "".join(texts).strip()
    if not reply:
        raise RunError("; ".join(errors) or f"OpenCode returned no reply {turn.stderr.strip()[-500:]}")
    return reply, f"{level}:{sid}" if sid else None


# Grok Build's permission modes and allow rules do not keep writes in the
# working folder on Windows, and its sandbox is Linux and macOS only.
# Below "full" it runs from an isolated home (no user config, plugins or
# memory; no Claude, Cursor or Codex hooks, skills and MCP servers) whose
# PreToolUse hook, grok_guard.py, denies every tool but reading (and, for
# "workspace", editing) inside the working folder. Deny rules for the
# shell, MCP and the protected folders back it up.
_GROK_ENV = {"GROK_MEMORY": "0", "GROK_DISABLE_AUTOUPDATER": "1"} | {
    f"GROK_{vendor}_{part}_ENABLED": "0"
    for vendor in ("CLAUDE", "CURSOR", "CODEX")
    for part in ("MCPS", "HOOKS", "SKILLS", "AGENTS", "RULES")
}
_GROK_COMPAT = "".join(
    f"\n[compat.{vendor}]\n" + "".join(f"{cell} = false\n" for cell in
                                       ("skills", "rules", "agents", "mcps", "hooks", "sessions"))
    for vendor in ("claude", "cursor")
)

GROK_CONFIG = """[cli]
use_leader = false
auto_update = false

[ui]
yolo = false
permission_mode = "default"

[memory]
enabled = false

[marketplace]
default_skills_installs_purged = true
official_marketplace_auto_installed = true

[[hooks.PreToolUse]]
hooks = [
  {{ type = "command", command = {python}, args = ["-I", "-X", "utf8", {guard}], timeout = 20 }},
]
""" + _GROK_COMPAT


def _grok_deny() -> list[str]:
    from .permissions import PROTECTED

    rules = ["Bash", "MCPTool", "WebFetch"]
    for folder in PROTECTED:
        rules += [f"{tool}({folder}/**)" for tool in ("Read", "Edit", "Write")]
    return [arg for rule in rules for arg in ("--deny", rule)]


def run_grok(
    turn: Turn, cfg: dict[str, Any], prompt: str, session: str | None, level: str, cwd: Path
) -> tuple[str, str | None]:
    resume = _level_session(session, level)
    fd, prompt_file = tempfile.mkstemp(prefix="a2a-grok-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    try:
        cmd = [_real_exe(cfg.get("command") or "grok"), "--prompt-file", prompt_file,
               "--output-format", "json", "--always-approve", "--no-subagents"]
        if cfg.get("model"):
            cmd += ["-m", cfg["model"]]
        if resume:
            cmd += ["-r", resume]
        env = None
        if level != "full":
            guard = Path(__file__).with_name("grok_guard.py")
            python = Path(sys.executable)
            if not guard.exists() or not python.exists():
                raise RunError("the Grok Build guard hook is missing")
            home = _isolated("grok")
            _write_if_changed(home / "config.toml", GROK_CONFIG.format(
                python=json.dumps(str(python)), guard=json.dumps(str(guard))))
            auth = Path(os.environ.get("GROK_AUTH_PATH") or Path.home() / ".grok" / "auth.json")
            env = {**_GROK_ENV, "GROK_HOME": str(home), "GROK_AUTH_PATH": str(auth),
                   "A2A_GUARD_LEVEL": level, "A2A_GUARD_ROOT": str(cwd)}
            cmd += _grok_deny()
        cmd += (cfg.get("args") or {}).get(level) or []
        out = turn.run(cmd, "", cwd, cfg.get("timeout", 1800), env)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(prompt_file)
    data = _json_tail(out, "Grok Build", turn)
    reply = str(data.get("text") or "").strip()
    if not reply:
        raise RunError(str(data.get("error") or turn.stderr.strip()[-500:] or "Grok Build returned no reply"))
    new = data.get("sessionId")
    return reply, f"{level}:{new}" if new else session


def run_window(
    turn: Turn, cfg: dict[str, Any], prompt: str, session: str | None, level: str, cwd: Path
) -> tuple[str, str | None]:
    """Deliver the message into a window the Administrator already has open.

    ``cfg["window"]`` names a helper: ``argv`` (a list that may use the
    placeholders {id}, {text}, {prompt_file}, {target} and {seconds}),
    ``reply`` ("text", "json" for a ``reply`` key, or "none") and an
    optional default ``target``. The window keeps its own permissions, so
    the server sends here only at "full".
    """
    win = cfg.get("window") or {}
    if not win.get("argv"):
        raise RunError("no window helper is configured for this agent")
    target = str(cfg.get("window_target") or win.get("target") or "")
    if any("{target}" in a for a in win["argv"]) and not target:
        raise RunError("no window named; pass a target")
    rid = f"A2A-{os.urandom(4).hex()}"
    text = f"{prompt}\n\nEnd your reply with DONE {rid}."
    fd, prompt_file = tempfile.mkstemp(prefix="a2a-window-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"AGENT MESSAGE [{rid}] from a2a-bridge:\n\n{text}")
    timeout = cfg.get("timeout", 1800)
    try:
        cmd = [a.format(id=rid, text=text, prompt_file=prompt_file, target=target,
                        seconds=int(timeout)) for a in win["argv"]]
        out = turn.run(cmd, "", cwd, timeout + 30)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(prompt_file)
    if turn.proc and turn.proc.returncode != 0:
        raise RunError(turn.stderr.strip()[-1000:] or out.strip()[-1000:] or "window helper failed")
    mode = win.get("reply", "text")
    if mode == "none":
        return f"Delivered to the open window as {rid}; its reply stays in that window.", target
    reply = out.strip()
    if mode == "json":
        try:
            reply = str(json.loads(reply.splitlines()[-1]).get("reply") or "")
        except (ValueError, IndexError, AttributeError) as exc:
            raise RunError(f"unexpected window helper output: {out[-500:]}") from exc
    reply = re.sub(r"\s*DONE " + re.escape(rid) + r"\.?\s*$", "", reply).strip()
    return reply or "(empty reply)", target


def run_echo(
    turn: Turn, cfg: dict[str, Any], prompt: str, session: str | None, level: str, cwd: Path
) -> tuple[str, str | None]:
    """Test runner: echoes the prompt with the level and turn number."""
    count = int(session or 0) + 1
    return f"echo[{level}#{count}@{cwd.name}]: {prompt.splitlines()[-1]}", str(count)


RUNNERS: dict[str, Callable[..., tuple[str, str | None]]] = {
    "claude": run_claude,
    "codex": run_codex,
    "agy": run_agy,
    "opencode": run_opencode,
    "grok": run_grok,
    "window": run_window,
    "echo": run_echo,
}
