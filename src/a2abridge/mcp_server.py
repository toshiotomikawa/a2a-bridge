"""MCP server over stdio (newline-delimited JSON-RPC), stdlib only.

Exposes the outbound side as tools, so Claude Code, Codex and any MCP
client can talk to an A2A peer. Every send becomes a background job:
the tool waits a little, then returns either the reply or the job id.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from . import __version__, config, jobs
from .client import A2AError, Client

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

TOOLS = [
    {
        "name": "a2a_send",
        "description": (
            "Send a message to an A2A agent (by default the configured peer, e.g. the "
            "Hermes agent) and get its reply. The conversation keeps context across "
            "calls: reuse the same 'conversation' name to continue, or set "
            "new_conversation to start over. Runs in the background: if the reply takes "
            "longer than wait_seconds, you get a job_id to check later with a2a_check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Text to send."},
                "peer": {"type": "string", "description": "Peer name; default from config."},
                "conversation": {
                    "type": "string",
                    "description": "Conversation name. Default: one per MCP session.",
                },
                "new_conversation": {"type": "boolean", "default": False},
                "wait_seconds": {
                    "type": "number",
                    "default": 50,
                    "description": "How long to wait for the reply before returning a job id (0 = return at once).",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "a2a_check",
        "description": "Get the status and reply of a background job from a2a_send, optionally waiting for it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "wait_seconds": {"type": "number", "default": 0},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "a2a_jobs",
        "description": "List recent background jobs (newest first) with their status.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "a2a_peers",
        "description": "List configured A2A peers, whether each is reachable, its name and skills, and saved conversations.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class McpServer:
    def __init__(self) -> None:
        self.default_conversation = "mcp-" + time.strftime("%Y%m%d-%H%M%S")

    def handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        method = req.get("method")
        rid = req.get("id")
        if rid is None:  # notification
            return None
        try:
            if method == "initialize":
                params = req.get("params") or {}
                client = (params.get("clientInfo") or {}).get("name")
                if client:
                    self.default_conversation = f"mcp-{client}-" + time.strftime("%Y%m%d-%H%M%S")
                asked = params.get("protocolVersion")
                result: Any = {
                    "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[1],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "a2a-bridge", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = req.get("params") or {}
                result = self.call(params.get("name"), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as exc:  # noqa: BLE001
            result = _text(f"error: {exc}", error=True)
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        cfg = config.load()
        if name == "a2a_send":
            peer = args.get("peer") or cfg["default_peer"]
            job = jobs.create_job(
                peer,
                args.get("conversation") or self.default_conversation,
                args["message"],
                new=bool(args.get("new_conversation")),
            )
            jobs.spawn_worker(job["id"])
            job = jobs.wait_job(job["id"], float(args.get("wait_seconds", 50)))
            return _job_result(job)
        if name == "a2a_check":
            job = jobs.wait_job(args["job_id"], float(args.get("wait_seconds", 0)))
            if job is None:
                return _text(f"no job {args['job_id']}", error=True)
            return _job_result(job)
        if name == "a2a_jobs":
            rows = [
                f"{j['id']}  {j['status']:<9}  {j['peer']}/{j['conversation']}  {j['message'][:60]!r}"
                for j in jobs.list_jobs(int(args.get("limit", 10)))
            ]
            return _text("\n".join(rows) or "no jobs")
        if name == "a2a_peers":
            out = []
            for pname, peer in cfg["peers"].items():
                try:
                    card = Client(peer["url"], config.peer_token(peer)).card()
                    skills = ", ".join(s.get("name", s.get("id", "")) for s in card.get("skills", []))
                    out.append(f"{pname}: {card.get('name')} at {peer['url']} (online). Skills: {skills}")
                except A2AError as exc:
                    out.append(f"{pname}: {peer['url']} (offline: {exc})")
            convs = jobs.conversations()
            if convs:
                out.append("Conversations: " + ", ".join(sorted(convs)))
            out.append(f"This session's default conversation: {self.default_conversation}")
            return _text("\n".join(out))
        return _text(f"unknown tool {name}", error=True)

    def run(self) -> None:
        stdin = open(sys.stdin.fileno(), "r", encoding="utf-8", closefd=False)
        stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False, newline="\n")
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            batch = req if isinstance(req, list) else [req]
            replies = [r for r in (self.handle(x) for x in batch) if r is not None]
            for reply in replies:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def _text(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _job_result(job: dict[str, Any] | None) -> dict[str, Any]:
    if job is None:
        return _text("job not found", error=True)
    head = f"job {job['id']} ({job['peer']}/{job['conversation']}): {job['status']}"
    if job["status"] == "completed":
        return _text(f"{head}\n\n{job['reply']}")
    if job["status"] == "failed":
        return _text(f"{head}\n\n{job['error']}", error=True)
    return _text(f"{head}. Still working; call a2a_check with this job_id later.")


def main() -> None:
    McpServer().run()
