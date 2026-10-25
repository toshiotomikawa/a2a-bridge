"""Conversations and background jobs for outbound messages.

A conversation is a named A2A ``contextId``; the id is chosen here, so
two agents never share a context by accident. A job is one message sent
to a peer; it can run inline or in a detached worker process, and its
result is written to ``state/jobs/<id>.json`` for any process to read.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .client import TERMINAL_STATES, A2AError, Client, strip_reasoning, task_reply

ACTIVE = {"queued", "running"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- conversations -------------------------------------------------------


def _conv_file() -> Path:
    return config.state_dir() / "conversations.json"


def conversations() -> dict[str, Any]:
    return config.read_json(_conv_file(), {})


def conversation(peer: str, name: str, new: bool = False) -> str:
    """Return the contextId for ``peer/name``, creating it when needed."""
    key = f"{peer}/{name}"
    convs = conversations()
    if new or key not in convs:
        convs[key] = {"context_id": f"ctx-{uuid.uuid4().hex}", "created": now(), "turns": 0}
        config.write_json(_conv_file(), convs)
    return convs[key]["context_id"]


def _touch_conversation(peer: str, name: str) -> None:
    key = f"{peer}/{name}"
    convs = conversations()
    if key in convs:
        convs[key]["turns"] = convs[key].get("turns", 0) + 1
        convs[key]["last_used"] = now()
        config.write_json(_conv_file(), convs)


def forget_conversation(peer: str, name: str) -> bool:
    convs = conversations()
    if convs.pop(f"{peer}/{name}", None) is None:
        return False
    config.write_json(_conv_file(), convs)
    return True


# --- jobs ----------------------------------------------------------------


def _job_file(job_id: str) -> Path:
    return config.state_dir("jobs") / f"{job_id}.json"


def load_job(job_id: str) -> dict[str, Any] | None:
    job = config.read_json(_job_file(job_id), None)
    if job and job["status"] in ACTIVE and job.get("pid") and not pid_alive(job["pid"]):
        job.update(status="failed", error="worker process died", finished=now())
        save_job(job)
    return job


def save_job(job: dict[str, Any]) -> None:
    config.write_json(_job_file(job["id"]), job)


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    files = sorted(config.state_dir("jobs").glob("*.json"), reverse=True)[:limit]
    return [j for j in (load_job(f.stem) for f in files) if j]


def create_job(peer: str, conversation_name: str, text: str, new: bool = False) -> dict[str, Any]:
    cfg = config.load()
    if peer not in cfg["peers"]:
        raise A2AError(f"unknown peer '{peer}'. Known: {', '.join(cfg['peers'])}")
    job = {
        "id": time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3),
        "peer": peer,
        "conversation": conversation_name,
        "context_id": conversation(peer, conversation_name, new=new),
        "message": text,
        "status": "queued",
        "created": now(),
        "reply": None,
        "error": None,
        "task_id": None,
        "pid": None,
    }
    save_job(job)
    return job


def _wait_turn(job: dict[str, Any]) -> None:
    """Keep turns of one conversation in order: wait for earlier active jobs."""
    while True:
        earlier = [
            j
            for j in list_jobs(limit=200)
            if j["context_id"] == job["context_id"]
            and j["id"] < job["id"]
            and j["status"] in ACTIVE
        ]
        if not earlier:
            return
        time.sleep(1)


def run_job(job_id: str) -> dict[str, Any]:
    job = load_job(job_id)
    if job is None:
        raise A2AError(f"no job {job_id}")
    _wait_turn(job)
    cfg = config.load()
    peer = cfg["peers"][job["peer"]]
    job.update(status="running", started=now(), pid=os.getpid())
    save_job(job)
    client = Client(peer["url"], config.peer_token(peer), timeout=peer.get("timeout", 900))
    try:
        task = client.send(job["message"], context_id=job["context_id"])
        job["task_id"] = task.get("id")
        # Peers that answer before finishing: poll until a final state.
        deadline = time.time() + peer.get("timeout", 900)
        while task.get("id") and task["status"]["state"] not in TERMINAL_STATES:
            if time.time() > deadline:
                raise A2AError("timed out waiting for the peer")
            time.sleep(2)
            task = client.get_task(task["id"])
        reply = task_reply(task)
        if peer.get("strip_reasoning", True):
            reply = strip_reasoning(reply)
        state = task["status"]["state"]
        ok = state in ("TASK_STATE_COMPLETED", "completed")
        job.update(status="completed" if ok else "failed", reply=reply, state=state)
        if not ok:
            job["error"] = f"peer ended the task as {state}"
        _touch_conversation(job["peer"], job["conversation"])
    except Exception as exc:  # noqa: BLE001 - every failure is reported in the job
        job.update(status="failed", error=str(exc))
    job["finished"] = now()
    save_job(job)
    return job


def spawn_worker(job_id: str) -> int:
    """Run the job in a detached process that outlives the caller."""
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-m", "a2abridge", "_worker", job_id], **kwargs)
    job = load_job(job_id)
    if job and job["status"] == "queued" and not job.get("pid"):
        job["pid"] = proc.pid
        save_job(job)
    return proc.pid


def wait_job(job_id: str, timeout: float | None = None) -> dict[str, Any] | None:
    deadline = None if timeout is None else time.time() + timeout
    while True:
        job = load_job(job_id)
        if job is None or job["status"] not in ACTIVE:
            return job
        if deadline is not None and time.time() >= deadline:
            return job
        time.sleep(0.5)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
