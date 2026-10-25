"""Command line: ``a2a <command>``. Run ``a2a -h`` for the list."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from typing import Any

from . import __version__, config, jobs, permissions
from .client import A2AError, Client


def _print_job(job: dict[str, Any] | None, as_json: bool) -> int:
    if job is None:
        print("job not found", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(job, ensure_ascii=False, indent=2))
    elif job["status"] == "completed":
        print(job["reply"])
    elif job["status"] == "failed":
        print(f"failed: {job['error']}", file=sys.stderr)
    else:
        print(f"{job['id']} {job['status']}")
    return {"completed": 0, "failed": 1}.get(job["status"], 2)


def cmd_init(args: argparse.Namespace) -> int:
    path, created = config.init(args.peer_name, args.peer_url)
    print(f"{'created' if created else 'already exists'}: {path}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.message == "-" else args.message
    cfg = config.load()
    job = jobs.create_job(args.peer or cfg["default_peer"], args.conversation, text, new=args.new)
    if args.background:
        jobs.spawn_worker(job["id"])
        print(job["id"])
        return 0
    return _print_job(jobs.run_job(job["id"]), args.json)


def cmd_wait(args: argparse.Namespace) -> int:
    return _print_job(jobs.wait_job(args.job, args.timeout), args.json)


def cmd_jobs(args: argparse.Namespace) -> int:
    for j in jobs.list_jobs(args.limit):
        print(f"{j['id']}  {j['status']:<9}  {j['peer']}/{j['conversation']}  {j['message'][:50]!r}")
    return 0


def cmd_conversations(args: argparse.Namespace) -> int:
    if args.forget:
        peer = args.peer or config.load()["default_peer"]
        ok = jobs.forget_conversation(peer, args.forget)
        print("forgotten" if ok else "not found")
        return 0 if ok else 1
    for key, conv in sorted(jobs.conversations().items()):
        print(f"{key}  turns={conv.get('turns', 0)}  last={conv.get('last_used', '-')}")
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    cfg = config.load()
    for name, peer in cfg["peers"].items():
        mark = "*" if name == cfg["default_peer"] else " "
        try:
            card = Client(peer["url"], config.peer_token(peer)).card()
            print(f"{mark} {name}: {card.get('name')} at {peer['url']} (online)")
        except A2AError:
            print(f"{mark} {name}: {peer['url']} (offline)")
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    cfg = config.load()
    if args.target.startswith("http"):
        client = Client(args.target)
    else:
        peer = cfg["peers"][args.target]
        client = Client(peer["url"], config.peer_token(peer))
    print(json.dumps(client.card(), ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    if args.log or sys.stdout is None:
        log = open(args.log or config.home() / "serve.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    serve()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    g = permissions.grant(args.agent, args.level, args.minutes, args.dir)
    until = datetime.fromtimestamp(g["expires"]).strftime("%H:%M")
    where = g["workdir"] or "any folder"
    print(f"{args.agent} may use '{args.level}' in {where} until {until}")
    return 0


def cmd_grants(args: argparse.Namespace) -> int:
    for g in permissions.grants():
        left = int((g["expires"] - time.time()) / 60)
        print(f"{g['agent']}: {g['level']} in {g['workdir'] or 'any folder'}, {left} min left")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    print(f"revoked {permissions.revoke(args.agent)} grant(s)")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    jobs.run_job(args.job)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="a2a", description="Local A2A v1.0 bridge.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create the config file with a fresh server token")
    s.add_argument("--peer-name", default="hermes")
    s.add_argument("--peer-url", default=None)
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("send", help="send a message to a peer and print the reply")
    s.add_argument("message", help="text, or - to read from stdin")
    s.add_argument("--peer")
    s.add_argument("-c", "--conversation", default="default")
    s.add_argument("--new", action="store_true", help="start the conversation over")
    s.add_argument("-b", "--background", action="store_true", help="print a job id and return")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("wait", help="wait for a background job and print its reply")
    s.add_argument("job")
    s.add_argument("--timeout", type=float, default=None, help="seconds; 0 = check once")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("jobs", help="list recent jobs")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_jobs)

    s = sub.add_parser("conversations", help="list or forget conversations")
    s.add_argument("--forget", metavar="NAME")
    s.add_argument("--peer")
    s.set_defaults(func=cmd_conversations)

    sub.add_parser("peers", help="list peers and whether they are online").set_defaults(func=cmd_peers)

    s = sub.add_parser("card", help="show the Agent Card of a peer name or URL")
    s.add_argument("target")
    s.set_defaults(func=cmd_card)

    s = sub.add_parser("serve", help="serve local agents (Claude Code, Codex) over A2A")
    s.add_argument("--log", default=None)
    s.set_defaults(func=cmd_serve)

    sub.add_parser("mcp", help="run the MCP server on stdio").set_defaults(func=cmd_mcp)

    s = sub.add_parser("grant", help="raise an agent's permission ceiling for a while")
    s.add_argument("agent")
    s.add_argument("level", choices=config.PERMISSION_LEVELS)
    s.add_argument("--dir", default=None, help="limit the grant to this folder")
    s.add_argument("--minutes", type=float, default=60)
    s.set_defaults(func=cmd_grant)

    sub.add_parser("grants", help="list active grants").set_defaults(func=cmd_grants)

    s = sub.add_parser("revoke", help="remove grants (all, or one agent's)")
    s.add_argument("agent", nargs="?")
    s.set_defaults(func=cmd_revoke)

    s = sub.add_parser("_worker")
    s.add_argument("job")
    s.set_defaults(func=cmd_worker)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except A2AError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
