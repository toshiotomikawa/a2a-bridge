"""Inbound A2A v1.0 server: lets an A2A agent call local coding agents.

Each configured agent (Claude Code, Codex, ...) gets its own port and
Agent Card, so any A2A client can address it by URL. An A2A
``contextId`` maps to the agent's own session id, so a caller keeps one
conversation across turns. Tasks and contexts belong to the peer that
created them; other peers can neither see nor continue them. Methods:
SendMessage, GetTask, ListTasks, CancelTask (plus the pre-1.0 aliases).
"""

from __future__ import annotations

import hmac
import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, config, permissions
from .client import parts_text
from .runners import RUNNERS, RunError, Turn

ALIASES = {
    "message/send": "SendMessage",
    "tasks/get": "GetTask",
    "tasks/list": "ListTasks",
    "tasks/cancel": "CancelTask",
}

MAX_TASKS = 500
MAX_BODY = 1_048_576
DONE = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
LOCAL_HOSTS = ("127.0.0.1", "localhost")

FRAME = (
    "[Message from the A2A peer '{peer}', relayed by a2a-bridge. "
    "Permission for this turn: {level}. Working folder: {folder}.{notes}]\n\n"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class AgentService:
    """Tasks and contexts for one served agent."""

    def __init__(self, name: str, cfg: dict[str, Any], server_cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.server_cfg = server_cfg
        self.runner = RUNNERS[cfg.get("runner", name)]
        self.tasks: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, Turn] = {}
        self.lock = threading.RLock()
        self.context_locks: dict[str, threading.Lock] = {}
        self.slots = threading.Semaphore(int(server_cfg.get("max_concurrent", 2)))
        self.contexts_file = config.state_dir("contexts") / f"{name}.json"

    # --- card ---------------------------------------------------------

    def card(self, base_url: str) -> dict[str, Any]:
        secured = bool(self.server_cfg.get("tokens"))
        return {
            "name": self.cfg.get("name", self.name),
            "description": (
                f"{self.cfg.get('name', self.name)} on this PC, reachable over A2A through "
                "a2a-bridge. Optional first lines '[permission: read|workspace|full]' and "
                "'[workdir: <folder>]' ask for a level and a folder; the owner's limits apply."
            ),
            "url": base_url,
            "version": __version__,
            "provider": {"organization": "a2a-bridge", "url": base_url},
            "supportedInterfaces": [
                {"url": base_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "stateTransitionHistory": False,
                "extendedAgentCard": False,
            },
            "securitySchemes": (
                {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}} if secured else {}
            ),
            "security": [{"bearer": []}] if secured else [],
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "coding",
                    "name": "coding",
                    "description": "Read, explain, review and change code and files.",
                    "tags": ["coding", "review", "files", "research"],
                }
            ],
        }

    # --- contexts (keyed by peer, so peers never share one) -----------

    def _context(self, key: str) -> dict[str, Any]:
        return config.read_json(self.contexts_file, {}).get(key, {})

    def _save_context(self, key: str, data: dict[str, Any]) -> None:
        with self.lock, config.locked(self.contexts_file):
            all_ctx = config.read_json(self.contexts_file, {})
            all_ctx[key] = data
            config.write_json(self.contexts_file, all_ctx)

    # --- methods ------------------------------------------------------

    def send_message(self, params: dict[str, Any], peer: str) -> dict[str, Any]:
        message = params.get("message")
        if not isinstance(message, dict):
            raise RpcError(-32602, "params.message must be an object")
        parts = message.get("parts")
        text = parts_text(parts if isinstance(parts, list) else [])
        body, _ = permissions.parse_directives(text)
        if not body.strip():
            raise RpcError(-32602, "message has no text")
        context_id = str(message.get("contextId") or f"ctx-{uuid.uuid4().hex}")
        conf = params.get("configuration")
        conf = conf if isinstance(conf, dict) else {}
        background = conf.get("returnImmediately") or conf.get("blocking") is False
        meta = message.get("metadata")
        task = {
            "id": f"task-{uuid.uuid4().hex[:16]}",
            "contextId": context_id,
            "status": {"state": "TASK_STATE_SUBMITTED", "timestamp": _now()},
            "metadata": {"peer": peer},
        }
        with self.lock:
            self.tasks[task["id"]] = task
            self.turns[task["id"]] = Turn()
            # Keep memory bounded: forget the oldest finished tasks.
            finished = [k for k, t in self.tasks.items() if t["status"]["state"] in DONE]
            for old in finished[: max(0, len(self.tasks) - MAX_TASKS)]:
                self.tasks.pop(old, None)
                self.turns.pop(old, None)
        worker = threading.Thread(
            target=self._execute,
            args=(task, text, meta if isinstance(meta, dict) else {}, peer),
            daemon=True,
        )
        worker.start()
        if not background:
            worker.join()
        return self._public(task)

    def _execute(self, task: dict[str, Any], text: str, meta: dict[str, Any], peer: str) -> None:
        key = f"{peer}:{task['contextId']}"
        with self.lock:
            ctx_lock = self.context_locks.setdefault(key, threading.Lock())
        with ctx_lock, self.slots:
            with self.lock:
                turn = self.turns.get(task["id"])
                if turn is None or turn.cancelled:
                    return
                self._set_state(task, "TASK_STATE_WORKING")
            body, directives = permissions.parse_directives(text)
            ctx = self._context(key)
            level, folder, notes = permissions.resolve(
                self.name,
                self.cfg,
                str(meta.get("permission") or directives.get("permission") or "") or None,
                str(meta.get("workdir") or directives.get("workdir") or "") or None,
                current=ctx.get("workdir"),
            )
            # A session is tied to its folder; a new folder starts a new session.
            session = ctx.get("session") if ctx.get("workdir") == str(folder) else None
            note_text = (" Notes: " + "; ".join(notes) + ".") if notes else ""
            prompt = FRAME.format(peer=peer, level=level, folder=folder, notes=note_text) + body
            try:
                try:
                    reply, new_session = self.runner(turn, self.cfg, prompt, session, level, folder)
                except RunError:
                    if not session or turn.cancelled:
                        raise
                    # The saved session may be gone; start a fresh one once.
                    notes.append("previous session could not be resumed; started a new one")
                    session = None
                    reply, new_session = self.runner(turn, self.cfg, prompt, None, level, folder)
            except Exception as exc:  # noqa: BLE001 - report, never crash the server
                with self.lock:
                    if task["status"]["state"] not in DONE:
                        state = "TASK_STATE_CANCELED" if turn.cancelled else "TASK_STATE_FAILED"
                        self._set_state(task, state, f"{self.name} failed: {exc}")
                return
            if new_session and new_session != session:
                self._log_dispatch(peer, new_session)
            self._save_context(
                key,
                {"session": new_session, "workdir": str(folder), "updated": _now(), "peer": peer},
            )
            if notes:
                reply = f"[a2a-bridge: {'; '.join(notes)}]\n\n{reply}"
            with self.lock:
                if turn.cancelled or task["status"]["state"] in DONE:
                    return
                task["metadata"].update(permission=level, workdir=str(folder))
                task["artifacts"] = [{
                    "artifactId": uuid.uuid4().hex,
                    "parts": [{"text": reply, "mediaType": "text/plain"}],
                }]
                self._set_state(task, "TASK_STATE_COMPLETED", reply)

    def _set_state(self, task: dict[str, Any], state: str, text: str | None = None) -> None:
        status: dict[str, Any] = {"state": state, "timestamp": _now()}
        if text is not None:
            status["message"] = {
                "role": "ROLE_AGENT",
                "parts": [{"text": text, "mediaType": "text/plain"}],
                "messageId": uuid.uuid4().hex,
                "contextId": task["contextId"],
            }
        task["status"] = status

    def _log_dispatch(self, peer: str, child: str) -> None:
        path = self.server_cfg.get("dispatch_log")
        if not path:
            return
        harness = {"claude": "claude-code"}.get(self.cfg.get("runner", self.name), self.name)
        line = {"by": f"a2a-bridge:{peer}", "child": child, "harness": harness, "at": _now()}
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

    def _public(self, task: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(task))

    def _own(self, params: dict[str, Any], peer: str) -> dict[str, Any]:
        task = self.tasks.get(str(params.get("id", "")))
        if not task or task["metadata"].get("peer") != peer:
            raise RpcError(-32001, "task not found")
        return task

    def get_task(self, params: dict[str, Any], peer: str) -> dict[str, Any]:
        with self.lock:
            return self._public(self._own(params, peer))

    def list_tasks(self, params: dict[str, Any], peer: str) -> dict[str, Any]:
        ctx = params.get("contextId")
        try:
            size = max(1, min(200, int(params.get("pageSize") or 50)))
        except (TypeError, ValueError):
            raise RpcError(-32602, "pageSize must be a number")
        with self.lock:
            found = [
                self._public(t)
                for t in self.tasks.values()
                if t["metadata"].get("peer") == peer and (not ctx or t["contextId"] == ctx)
            ]
        found.reverse()
        return {"tasks": found[:size]}

    def cancel_task(self, params: dict[str, Any], peer: str) -> dict[str, Any]:
        with self.lock:
            task = self._own(params, peer)
            if task["status"]["state"] in DONE:
                raise RpcError(-32002, "task cannot be canceled")
            turn = self.turns.get(task["id"])
            self._set_state(task, "TASK_STATE_CANCELED", "canceled by the caller")
        if turn:
            turn.cancel()
        return self._public(task)


def make_handler(service: AgentService, tokens: dict[str, str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "a2a-bridge/" + __version__
        timeout = 30  # seconds per socket read; long turns run on worker threads

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

        def _send(self, code: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _rpc_error(self, rid: Any, code: int, message: str) -> None:
            self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

        def _peer(self) -> str | None:
            if not tokens:
                return "local"
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else ""
            for name, token in tokens.items():
                if given and hmac.compare_digest(given.encode(), token.encode()):
                    return name
            return None

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                host, port = self.server.server_address[:2]
                if host in ("0.0.0.0", ""):
                    host = (self.headers.get("Host") or "127.0.0.1").split(":")[0]
                self._send(200, service.card(f"http://{host}:{port}/"))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            peer = self._peer()
            if peer is None:
                self._send(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._send(413, {"error": "request too large"})
                return
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, UnicodeDecodeError):
                self._rpc_error(None, -32700, "parse error")
                return
            if not isinstance(req, dict):
                self._rpc_error(None, -32600, "invalid request")
                return
            rid = req.get("id")
            raw = req.get("method")
            method = ALIASES.get(raw, raw)
            params = req.get("params") if req.get("params") is not None else {}
            if not isinstance(params, dict):
                self._rpc_error(rid, -32602, "params must be an object")
                return
            try:
                if method == "SendMessage":
                    task = service.send_message(params, peer)
                    result: Any = dict(task, kind="task") if raw == "message/send" else {"task": task}
                elif method == "GetTask":
                    result = service.get_task(params, peer)
                elif method == "ListTasks":
                    result = service.list_tasks(params, peer)
                elif method == "CancelTask":
                    result = service.cancel_task(params, peer)
                elif method in ("SendStreamingMessage", "message/stream", "SubscribeToTask"):
                    raise RpcError(-32004, "streaming is not supported")
                else:
                    raise RpcError(-32601, f"method not found: {raw}")
            except RpcError as exc:
                self._rpc_error(rid, exc.code, str(exc))
                return
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    return Handler


def build_servers(cfg: dict[str, Any] | None = None) -> list[ThreadingHTTPServer]:
    cfg = cfg or config.load()
    server_cfg = cfg["server"]
    host = server_cfg.get("host", "127.0.0.1")
    tokens = server_cfg.get("tokens") or {}
    if host not in LOCAL_HOSTS and not tokens:
        raise SystemExit("refusing to listen beyond localhost without a token")
    servers = []
    for name, agent_cfg in server_cfg["agents"].items():
        if not agent_cfg.get("enabled", True):
            continue
        service = AgentService(name, agent_cfg, server_cfg)
        httpd = ThreadingHTTPServer((host, int(agent_cfg["port"])), make_handler(service, tokens))
        httpd.daemon_threads = True
        httpd.service = service  # type: ignore[attr-defined]
        servers.append(httpd)
    return servers


def serve(cfg: dict[str, Any] | None = None) -> None:
    servers = build_servers(cfg)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for s, t in zip(servers, threads):
        t.start()
        host, port = s.server_address[:2]
        print(f"{s.service.name}: http://{host}:{port}/", flush=True)  # type: ignore[attr-defined]
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()
