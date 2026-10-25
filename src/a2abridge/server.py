"""Inbound A2A v1.0 server: lets an A2A agent call local coding agents.

Each configured agent (Claude Code, Codex, ...) gets its own port and
Agent Card, so any A2A client can address it by URL. An A2A
``contextId`` maps to the agent's own session id, so a caller keeps one
conversation across turns. Methods: SendMessage, GetTask, ListTasks,
CancelTask (plus the pre-1.0 aliases).
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
DONE = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}

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
        self.lock = threading.Lock()
        self.context_locks: dict[str, threading.Lock] = {}
        self.slots = threading.Semaphore(int(server_cfg.get("max_concurrent", 2)))
        self.contexts_file = config.state_dir("contexts") / f"{name}.json"

    # --- card ---------------------------------------------------------

    def card(self, base_url: str) -> dict[str, Any]:
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
                {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}
                if self.server_cfg.get("tokens")
                else {}
            ),
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

    # --- contexts -----------------------------------------------------

    def _context(self, context_id: str) -> dict[str, Any]:
        return config.read_json(self.contexts_file, {}).get(context_id, {})

    def _save_context(self, context_id: str, data: dict[str, Any]) -> None:
        with self.lock:
            all_ctx = config.read_json(self.contexts_file, {})
            all_ctx[context_id] = data
            config.write_json(self.contexts_file, all_ctx)

    # --- methods ------------------------------------------------------

    def send_message(self, params: dict[str, Any], peer: str) -> dict[str, Any]:
        message = params.get("message") or {}
        text = parts_text(message.get("parts"))
        if not text.strip():
            raise RpcError(-32602, "message has no text")
        context_id = message.get("contextId") or f"ctx-{uuid.uuid4().hex}"
        task_id = f"task-{uuid.uuid4().hex[:16]}"
        conf = params.get("configuration") or {}
        background = conf.get("returnImmediately") or conf.get("blocking") is False
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "TASK_STATE_SUBMITTED", "timestamp": _now()},
            "history": [message],
            "metadata": {"peer": peer},
        }
        with self.lock:
            self.tasks[task_id] = task
            self.turns[task_id] = Turn()
            # Keep memory bounded: forget the oldest finished tasks.
            finished = [k for k, t in self.tasks.items() if t["status"]["state"] in DONE]
            for old in finished[: max(0, len(self.tasks) - MAX_TASKS)]:
                self.tasks.pop(old, None)
                self.turns.pop(old, None)
        worker = threading.Thread(
            target=self._execute, args=(task, text, message.get("metadata") or {}, peer),
            daemon=True,
        )
        worker.start()
        if not background:
            worker.join()
        return self._public(task)

    def _execute(self, task: dict[str, Any], text: str, meta: dict[str, Any], peer: str) -> None:
        context_id = task["contextId"]
        with self.lock:
            ctx_lock = self.context_locks.setdefault(context_id, threading.Lock())
        with ctx_lock, self.slots:
            turn = self.turns[task["id"]]
            if turn.cancelled:
                return
            self._set_state(task, "TASK_STATE_WORKING")
            body, directives = permissions.parse_directives(text)
            ctx = self._context(context_id)
            level, folder, notes = permissions.resolve(
                self.name,
                self.cfg,
                meta.get("permission") or directives.get("permission"),
                meta.get("workdir") or directives.get("workdir") or ctx.get("workdir"),
            )
            # A session is tied to its folder; a new folder starts a new session.
            session = ctx.get("session") if ctx.get("workdir") == str(folder) else None
            note_text = (" Notes: " + "; ".join(notes) + ".") if notes else ""
            prompt = FRAME.format(peer=peer, level=level, folder=folder, notes=note_text) + body
            try:
                reply, new_session = self.runner(turn, self.cfg, prompt, session, level, folder)
            except RunError as exc:
                state = "TASK_STATE_CANCELED" if turn.cancelled else "TASK_STATE_FAILED"
                self._set_state(task, state, f"{self.name} failed: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - report, never crash the server
                self._set_state(task, "TASK_STATE_FAILED", f"{self.name} failed: {exc}")
                return
            if new_session and new_session != session:
                self._log_dispatch(peer, new_session)
            self._save_context(
                context_id,
                {"session": new_session, "workdir": str(folder), "updated": _now(), "peer": peer},
            )
            if notes:
                reply = f"[a2a-bridge: {'; '.join(notes)}]\n\n{reply}"
            task["metadata"].update(permission=level, workdir=str(folder))
            task["artifacts"] = [
                {"artifactId": uuid.uuid4().hex, "parts": [{"text": reply, "mediaType": "text/plain"}]}
            ]
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
        return {k: v for k, v in task.items() if k != "history"}

    def get_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self.tasks.get(params.get("id", ""))
        if not task:
            raise RpcError(-32001, "task not found")
        return self._public(task)

    def list_tasks(self, params: dict[str, Any]) -> dict[str, Any]:
        ctx = params.get("contextId")
        found = [self._public(t) for t in self.tasks.values() if not ctx or t["contextId"] == ctx]
        found.reverse()
        return {"tasks": found[: int(params.get("pageSize") or 50)]}

    def cancel_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self.tasks.get(params.get("id", ""))
        if not task:
            raise RpcError(-32001, "task not found")
        if task["status"]["state"] in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
            raise RpcError(-32002, "task cannot be canceled")
        self.turns[task["id"]].cancel()
        self._set_state(task, "TASK_STATE_CANCELED", "canceled by the caller")
        return self._public(task)


def make_handler(service: AgentService, tokens: dict[str, str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "a2a-bridge/" + __version__

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

        def _send(self, code: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _peer(self) -> str | None:
            if not tokens:
                return "local"
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else ""
            for name, token in tokens.items():
                if given and hmac.compare_digest(given, token):
                    return name
            return None

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                host, port = self.server.server_address[:2]
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
                req = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send(200, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}})
                return
            rid = req.get("id")
            method = ALIASES.get(req.get("method"), req.get("method"))
            params = req.get("params") or {}
            try:
                if method == "SendMessage":
                    result: Any = {"task": service.send_message(params, peer)}
                    if req.get("method") == "message/send":
                        result = dict(result["task"], kind="task")
                elif method == "GetTask":
                    result = service.get_task(params)
                elif method == "ListTasks":
                    result = service.list_tasks(params)
                elif method == "CancelTask":
                    result = service.cancel_task(params)
                elif method in ("SendStreamingMessage", "message/stream", "SubscribeToTask"):
                    raise RpcError(-32004, "streaming is not supported")
                else:
                    raise RpcError(-32601, f"method not found: {req.get('method')}")
                self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})
            except RpcError as exc:
                self._send(200, {"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": exc.code, "message": str(exc)}})

    return Handler


def build_servers(cfg: dict[str, Any] | None = None) -> list[ThreadingHTTPServer]:
    cfg = cfg or config.load()
    server_cfg = cfg["server"]
    host = server_cfg.get("host", "127.0.0.1")
    tokens = server_cfg.get("tokens") or {}
    if host not in ("127.0.0.1", "localhost", "::1") and not tokens:
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
