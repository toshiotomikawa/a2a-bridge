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
OTHER = "other-token"


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Not under the system temp folder: AppData is a protected folder.
        cls.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        os.environ["A2A_HOME"] = cls.tmp.name
        cls.work = Path(cls.tmp.name) / "work"
        cls.work.mkdir()
        cls.port = free_port()
        cfg = {
            "default_peer": "echo",
            "peers": {"echo": {"url": f"http://127.0.0.1:{cls.port}", "token": TOKEN, "timeout": 30}},
            "server": {
                "tokens": {"tester": TOKEN, "other": OTHER},
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
                        "window": {"argv": [sys.executable, "-c",
                                            "import sys; print('window', sys.argv[1], 'DONE', sys.argv[2])",
                                            "{target}", "{id}"]},
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

    def test_peers_are_isolated(self) -> None:
        mine = self.client().send("private")
        other = self.client(token=OTHER)
        with self.assertRaises(Exception):
            other.get_task(mine["id"])
        self.assertEqual(other.rpc("ListTasks", {})["tasks"], [])
        # Same contextId from another peer is a separate conversation.
        t = other.send("hello", context_id=mine["contextId"])
        self.assertIn("#1", t["status"]["message"]["parts"][0]["text"])

    def test_malformed_requests(self) -> None:
        for body in (b"[]", b"1", b'{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":[]}'):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/", body,
                {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertIn("error", json.load(resp))
        with self.assertRaises(Exception):
            self.client().send("[permission: read]")  # directive only, no text

    def test_card_declares_security(self) -> None:
        self.assertEqual(self.client().card()["security"], [{"bearer": []}])

    def test_window_mode_needs_full(self) -> None:
        c = self.client()
        t = c.send("[mode: window]\n[target: W1]\nhello")
        text = t["status"]["message"]["parts"][0]["text"]
        self.assertIn("window mode needs 'full'", text)
        self.assertIn("echo[read", text)
        permissions.grant("echo", "full", 5)
        try:
            t = c.send("[permission: full]\n[mode: window]\n[target: W1]\nhello")
            self.assertEqual(t["status"]["message"]["parts"][0]["text"], "window W1")
            self.assertEqual(t["metadata"]["mode"], "window")
            # The same conversation keeps its window.
            t = c.send("[permission: full]\n[mode: window]\nagain", context_id=t["contextId"])
            self.assertEqual(t["status"]["message"]["parts"][0]["text"], "window W1")
        finally:
            permissions.revoke("echo")


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        os.environ.update(A2A_GUARD_ROOT=str(self.root), A2A_GUARD_LEVEL="workspace")

    def tearDown(self) -> None:
        for key in ("A2A_GUARD_ROOT", "A2A_GUARD_LEVEL"):
            os.environ.pop(key, None)
        self.root.rmdir()

    def check(self, tool: str, **tool_input: object) -> str | None:
        from a2abridge import grok_guard

        return grok_guard.decide({"toolName": tool, "toolInput": tool_input, "cwd": str(self.root)})

    def test_inside_is_allowed(self) -> None:
        self.assertIsNone(self.check("write", file_path="made.txt", content="hi"))
        self.assertIsNone(self.check("list_dir", target_directory=str(self.root)))
        self.assertIsNone(self.check("grep", pattern="C:/Users/anything"))

    def test_outside_and_other_tools_are_denied(self) -> None:
        self.assertTrue(self.check("write", file_path=str(self.root.parent / "x.txt")))
        self.assertTrue(self.check("read_file", target_file="../x.txt"))
        self.assertTrue(self.check("read_file", target_file=r"\\192.0.2.1\share\x"))
        self.assertTrue(self.check("list_dir", glob_pattern=str(self.root.parent / "*")))
        self.assertTrue(self.check("run_terminal_command", command="echo hi"))
        self.assertTrue(self.check("github__search", query="x"))

    def test_read_level_and_missing_setup(self) -> None:
        os.environ["A2A_GUARD_LEVEL"] = "read"
        self.assertTrue(self.check("write", file_path="made.txt"))
        self.assertIsNone(self.check("read_file", target_file="made.txt"))
        os.environ.pop("A2A_GUARD_ROOT")
        self.assertTrue(self.check("read_file", target_file="made.txt"))

    def test_control_files_drive_relative_and_grep_glob(self) -> None:
        for name in (".git/config", ".grok/hooks/x.json", ".opencode/plugins/x.js", ".env",
                     ".env.local", "opencode.json", "sub/.claude/settings.json"):
            self.assertTrue(self.check("write", file_path=name, content="x"), name)
        self.assertIsNone(self.check("read_file", target_file=".env.example.txt"))
        self.assertTrue(self.check("read_file", target_file="C:foo.txt"))
        self.assertIsNone(self.check("grep", pattern="x", glob="/*.py"))
        self.assertTrue(self.check("grep", pattern="x", glob="../*.py"))

    def test_hook_reads_utf8_paths(self) -> None:
        folder = self.root / "relatório"
        folder.mkdir()
        try:
            event = {"toolName": "write", "cwd": str(folder),
                     "toolInput": {"file_path": str(folder / "ação.txt"), "content": "ok"}}
            guard = SRC / "a2abridge" / "grok_guard.py"
            env = dict(os.environ, A2A_GUARD_ROOT=str(folder))
            proc = subprocess.run([sys.executable, "-I", str(guard)], env=env, timeout=30,
                                  input=(json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"),
                                  capture_output=True)
            self.assertEqual(json.loads(proc.stdout)["decision"], "allow", proc.stdout)
        finally:
            folder.rmdir()

    def test_hook_denies_on_bad_input(self) -> None:
        guard = SRC / "a2abridge" / "grok_guard.py"
        proc = subprocess.run([sys.executable, "-I", str(guard)], input="not json\n",
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["decision"], "deny")


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

    def test_read_grant_does_not_unlock_writing(self) -> None:
        agent = {"max_permission": "workspace", "default_workdir": str(self.work), "workdirs": []}
        permissions.grant("x", "read", 5, str(self.work))
        level, _, _ = permissions.resolve("x", agent, "workspace", str(self.work))
        self.assertEqual(level, "read")

    def test_folder_grant_is_capped_at_its_level(self) -> None:
        agent = {"max_permission": "full", "default_workdir": str(self.work), "workdirs": []}
        permissions.grant("x", "workspace", 5, str(self.work))
        level, _, _ = permissions.resolve("x", agent, "full", str(self.work))
        self.assertEqual(level, "workspace")

    def test_protected_and_network_folders(self) -> None:
        agent = {"max_permission": "workspace", "default_workdir": str(self.work),
                 "workdirs": [str(Path.home())]}
        for asked in (str(Path.home()), str(Path.home() / ".claude"), r"\\192.0.2.1\share",
                      r" \/192.0.2.1\share", "/\\192.0.2.1/share"):
            level, folder, notes = permissions.resolve("x", agent, "workspace", asked)
            self.assertEqual(folder, self.work.resolve(), asked)
            self.assertTrue(notes)

    def test_rejected_folder_keeps_current(self) -> None:
        agent = {"max_permission": "read", "default_workdir": str(self.work), "workdirs": []}
        current = self.work / "a"
        current.mkdir(exist_ok=True)
        _, folder, _ = permissions.resolve("x", agent, None, str(Path.home()), current=str(current))
        self.assertEqual(folder, current.resolve())
        # Asking for full in a protected folder without a full grant also keeps it.
        _, folder, _ = permissions.resolve("x", agent, "full", str(Path.home() / ".claude"),
                                           current=str(current))
        self.assertEqual(folder, current.resolve())


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
        nested = "💭 **Reasoning:**\n```\nplan:\n```python\nx = 1\n```\ndone\n```\n\nAnswer"
        self.assertEqual(strip_reasoning(nested), "Answer")

    def test_same_second_jobs_keep_order(self) -> None:
        ids = [jobs.create_job("echo", "order", str(i))["id"] for i in range(5)]
        self.assertEqual(ids, sorted(ids))


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
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
