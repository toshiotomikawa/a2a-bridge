"""End-to-end tests with the echo runner. Run: python -m unittest discover tests"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from a2abridge import config, jobs, permissions, server  # noqa: E402
from a2abridge.client import Client, strip_reasoning  # noqa: E402

TOKEN = "test-token"


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["A2A_HOME"] = cls.tmp.name
        cls.work = Path(cls.tmp.name) / "work"
        cls.work.mkdir()
        cls.port = free_port()
        cfg = {
            "default_peer": "echo",
            "peers": {"echo": {"url": f"http://127.0.0.1:{cls.port}", "token": TOKEN, "timeout": 30}},
            "server": {
                "tokens": {"tester": TOKEN},
                "dispatch_log": str(Path(cls.tmp.name) / "dispatch.jsonl"),
                "agents": {
                    "claude": {"enabled": False},
                    "codex": {"enabled": False},
                    "echo": {
                        "port": cls.port,
                        "runner": "echo",
                        "default_permission": "read",
                        "max_permission": "workspace",
                        "default_workdir": str(cls.work),
                        "workdirs": [str(cls.work)],
                    },
                },
            },
        }
        config.write_json(config.config_path(), cfg)
        cls.servers = server.build_servers()
        for s in cls.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        for s in cls.servers:
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    def client(self, token: str | None = TOKEN) -> Client:
        return Client(f"http://127.0.0.1:{self.port}", token, timeout=30)


class ServerTests(Base):
    def test_card(self) -> None:
        card = self.client().card()
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")

    def test_auth_required(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.client(token="wrong").send("hi")
        self.assertIn("401", str(ctx.exception))

    def test_context_continues(self) -> None:
        c = self.client()
        t1 = c.send("first")
        t2 = c.send("second", context_id=t1["contextId"])
        self.assertIn("#1", t1["status"]["message"]["parts"][0]["text"])
        self.assertIn("#2", t2["status"]["message"]["parts"][0]["text"])
        self.assertIn("second", t2["status"]["message"]["parts"][0]["text"])
        lines = Path(os.environ["A2A_HOME"], "dispatch.jsonl").read_text().splitlines()
        self.assertTrue(any(json.loads(x)["by"] == "a2a-bridge:tester" for x in lines))

    def test_permission_directive_and_cap(self) -> None:
        c = self.client()
        t = c.send(f"[permission: workspace]\n[workdir: {self.work}]\ndo it")
        self.assertIn("echo[workspace", t["status"]["message"]["parts"][0]["text"])
        t = c.send("[permission: full]\ndo more")
        text = t["status"]["message"]["parts"][0]["text"]
        self.assertIn("allowed up to 'workspace'", text)

    def test_return_immediately_and_get_task(self) -> None:
        c = self.client()
        res = c.rpc("SendMessage", {
            "message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "bg"}]},
            "configuration": {"returnImmediately": True},
        })
        task_id = res["task"]["id"]
        for _ in range(50):
            task = c.get_task(task_id)
            if task["status"]["state"] == "TASK_STATE_COMPLETED":
                break
            time.sleep(0.1)
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")

    def test_unknown_method(self) -> None:
        with self.assertRaises(Exception):
            self.client().rpc("Nope", {})


class PermissionTests(Base):
    def tearDown(self) -> None:
        permissions.revoke()

    def test_directives(self) -> None:
        body, found = permissions.parse_directives("[permission: full]\n[workdir: C:/x]\n\nhello")
        self.assertEqual(found, {"permission": "full", "workdir": "C:/x"})
        self.assertEqual(body, "hello")

    def test_grant_raises_ceiling(self) -> None:
        agent = {"default_permission": "read", "max_permission": "read",
                 "default_workdir": str(self.work), "workdirs": []}
        level, _, notes = permissions.resolve("x", agent, "full", None)
        self.assertEqual(level, "read")
        permissions.grant("x", "full", 5, str(self.work))
        level, _, _ = permissions.resolve("x", agent, "full", None)
        self.assertEqual(level, "full")
        permissions.revoke("x")
        level, _, _ = permissions.resolve("x", agent, "full", None)
        self.assertEqual(level, "read")

    def test_unlisted_folder_falls_back_to_read(self) -> None:
        agent = {"max_permission": "workspace", "default_workdir": str(self.work), "workdirs": []}
        level, _, notes = permissions.resolve("x", agent, "workspace", None)
        self.assertEqual(level, "read")
        self.assertTrue(notes)


class OutboundTests(Base):
    def test_inline_job_and_conversation(self) -> None:
        j1 = jobs.run_job(jobs.create_job("echo", "conv-a", "one")["id"])
        j2 = jobs.run_job(jobs.create_job("echo", "conv-a", "two")["id"])
        self.assertEqual(j1["status"], "completed", j1)
        self.assertIn("#2", j2["reply"])
        j3 = jobs.run_job(jobs.create_job("echo", "conv-a", "three", new=True)["id"])
        self.assertIn("#1", j3["reply"])

    def test_background_worker(self) -> None:
        job = jobs.create_job("echo", "conv-bg", "later")
        jobs.spawn_worker(job["id"])
        done = jobs.wait_job(job["id"], 30)
        self.assertEqual(done["status"], "completed", done)
        self.assertIn("later", done["reply"])

    def test_strip_reasoning(self) -> None:
        raw = "💭 **Reasoning:**\n```\nthinking\n```\n\nOK"
        self.assertEqual(strip_reasoning(raw), "OK")
        self.assertEqual(strip_reasoning("plain"), "plain")


class McpTests(Base):
    def test_mcp_roundtrip(self) -> None:
        env = dict(os.environ, PYTHONPATH=str(SRC))
        proc = subprocess.Popen(
            [sys.executable, "-m", "a2abridge", "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
            encoding="utf-8",
        )

        def call(rid: int, method: str, params: dict) -> dict:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                         "params": params}) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        try:
            init = call(1, "initialize", {"protocolVersion": "2025-06-18",
                                          "clientInfo": {"name": "test"}, "capabilities": {}})
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            tools = call(2, "tools/list", {})
            self.assertIn("a2a_send", [t["name"] for t in tools["result"]["tools"]])
            res = call(3, "tools/call", {"name": "a2a_send",
                                         "arguments": {"message": "via mcp", "wait_seconds": 30}})
            text = res["result"]["content"][0]["text"]
            self.assertIn("completed", text)
            self.assertIn("via mcp", text)
        finally:
            proc.stdin.close()
            proc.wait(10)


if __name__ == "__main__":
    unittest.main()
