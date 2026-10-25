"""Minimal A2A v1.0 JSON-RPC client (stdlib only)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

_REASONING_HEAD = re.compile(r"^\s*💭\s*\*\*Reasoning:\*\*\s*\n```[^\n]*\n")

TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "completed",
    "failed",
    "canceled",
    "rejected",
}


class A2AError(Exception):
    pass


def strip_reasoning(text: str) -> str:
    """Drop the reasoning block some agents (Hermes) put before the answer.

    The block is a fence; fences opened inside it (```python ... ```) are
    skipped, so code in the reasoning does not end the block early.
    """
    head = _REASONING_HEAD.match(text)
    if not head:
        return text
    lines = text[head.end():].split("\n")
    inner = False
    for i, line in enumerate(lines):
        fence = line.strip()
        if not fence.startswith("```"):
            continue
        if fence == "```" and not inner:
            return "\n".join(lines[i + 1:]).lstrip()
        inner = not inner if fence == "```" else True
    return text  # unbalanced: keep everything rather than lose the answer


def parts_text(parts: list[dict[str, Any]] | None) -> str:
    """Join the text parts of a message (v1.0 and pre-1.0 shapes)."""
    out = []
    for part in parts or []:
        if "text" in part:
            out.append(part["text"])
        elif part.get("kind") == "text":
            out.append(part.get("text", ""))
    return "\n".join(out)


def task_reply(task: dict[str, Any]) -> str:
    """Best-effort final text of a task: artifacts, else the status message."""
    text = "\n".join(
        t for t in (parts_text(a.get("parts")) for a in task.get("artifacts") or []) if t
    )
    if text:
        return text
    message = (task.get("status") or {}).get("message") or {}
    return parts_text(message.get("parts"))


class Client:
    def __init__(self, url: str, token: str | None = None, timeout: float = 900):
        self.url = url.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def card(self) -> dict[str, Any]:
        req = urllib.request.Request(
            self.url + ".well-known/agent-card.json", headers=self._headers()
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError) as exc:
            raise A2AError(f"cannot reach {self.url}: {exc}") from exc

    def rpc(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        req = urllib.request.Request(
            self.url, json.dumps(body).encode("utf-8"), self._headers()
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            # Peers often put a JSON-RPC error in the body of a 4xx/5xx reply.
            detail = ""
            try:
                err = json.loads(exc.read() or b"{}").get("error")
                if isinstance(err, dict):
                    err = err.get("message", err)
                if err:
                    detail = f": {err}"
            except (ValueError, AttributeError):
                pass
            finally:
                exc.close()
            raise A2AError(f"HTTP {exc.code} from {self.url}{detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise A2AError(f"cannot reach {self.url}: {exc}") from exc
        if "error" in data:
            err = data["error"]
            raise A2AError(f"{err.get('code')}: {err.get('message')}")
        return data.get("result")

    def send(
        self,
        text: str,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """SendMessage and return the Task (or a Task-shaped wrapper of a Message)."""
        message: dict[str, Any] = {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
        if context_id:
            message["contextId"] = context_id
        if metadata:
            message["metadata"] = metadata
        result = self.rpc("SendMessage", {"message": message}, timeout=timeout)
        if isinstance(result, dict) and "task" in result:
            return result["task"]
        if isinstance(result, dict) and "message" in result:
            msg = result["message"]
            return {
                "id": None,
                "contextId": msg.get("contextId", context_id),
                "status": {"state": "TASK_STATE_COMPLETED", "message": msg},
            }
        return result

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.rpc("GetTask", {"id": task_id}, timeout=30)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return self.rpc("CancelTask", {"id": task_id}, timeout=30)
