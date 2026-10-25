# a2a-bridge

A small local bridge for the [A2A protocol](https://a2a-protocol.org) (v1.0, HTTP + JSON-RPC).
It works in both directions on one PC:

- **Outbound.** Coding agents such as Claude Code and Codex (or any script) send messages to
  an A2A agent, for example a [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a),
  and get replies in the background, keeping the conversation's context.
- **Inbound.** The A2A agent can call Claude Code and Codex back. Each one is served on its
  own local port with its own Agent Card, and runs in non-interactive mode under permission
  limits set by the PC's owner.

Pure Python standard library, no dependencies. Python 3.10 or newer.

## Install

```bash
uv venv ~/.a2a/.venv
uv pip install --python ~/.a2a/.venv .
~/.a2a/.venv/Scripts/a2a init          # Windows; on Linux/macOS use .venv/bin/a2a
```

`a2a init` writes `~/.a2a/config.json` with one peer (default `http://127.0.0.1:9900`) and a
random token for the inbound server. Set `A2A_HOME` to use another folder. Config, tokens,
conversations and jobs all live there, never in this repository.

## Outbound: talk to an A2A agent

Command line:

```bash
a2a send "Summarize today's notes"                 # waits and prints the reply
a2a send -c research "Now compare with last week"  # named conversation keeps context
a2a send --new -c research "Start over"            # new context for that name
a2a send -b "Long task"                            # prints a job id at once
a2a wait <job-id>                                  # waits, prints the reply
a2a jobs | a2a conversations | a2a peers | a2a card hermes
```

MCP server (stdio), for Claude Code, Codex and other MCP clients:

```bash
claude mcp add -s user a2a -- ~/.a2a/.venv/Scripts/a2a mcp
codex mcp add a2a -- ~/.a2a/.venv/Scripts/a2a mcp
```

Tools: `a2a_send` (waits up to `wait_seconds`, then hands back a job id), `a2a_check`,
`a2a_jobs`, `a2a_peers`. Each MCP session gets its own default conversation; pass
`conversation` to share or resume one.

Some agents put a reasoning block before the answer; it is removed unless the peer sets
`"strip_reasoning": false`.

## Inbound: let the A2A agent call Claude Code and Codex

```bash
a2a serve            # claude on 127.0.0.1:9901, codex on 127.0.0.1:9902
```

On Windows, start it at login without a console window with a small `.vbs` file in the
Startup folder:

```vb
Set s = CreateObject("WScript.Shell")
s.Run """<home>\.a2a\.venv\Scripts\a2a.exe"" serve --log ""<home>\.a2a\serve.log""", 0, False
```

Register the two endpoints as peers in the calling agent. For Hermes, put the server token
from `~/.a2a/config.json` in its `.env` (for example `A2A_BRIDGE_TOKEN=...`) and reference it
from `config.yaml`:

```yaml
a2a_agents:
  claude:
    url: "http://127.0.0.1:9901"
    auth: { type: bearer, token: "${A2A_BRIDGE_TOKEN}" }
    timeout: 1800
    capabilities: [coding, review]
  codex:
    url: "http://127.0.0.1:9902"
    auth: { type: bearer, token: "${A2A_BRIDGE_TOKEN}" }
    timeout: 1800
    capabilities: [coding, review]
```

To require a token on the Hermes side too, set `A2A_PEER_TOKENS="a2a-bridge:<token>"` in
its `.env` and the same token as the peer's `token` in `~/.a2a/config.json`.

Each A2A `contextId` maps to one Claude Code session or Codex thread, so follow-up messages
continue the same conversation. Tasks and conversations belong to the peer (token) that
created them: another peer can neither list, read, cancel nor continue them. If a saved
session can no longer be resumed, the next turn starts a fresh one and says so. Methods:
`SendMessage` (blocking, or `returnImmediately`), `GetTask`, `ListTasks`, `CancelTask`, plus
the pre-1.0 aliases.

### Permissions

There are three levels:

| Level | Claude Code | Codex |
| --- | --- | --- |
| `read` | `--restricted`: read and search tools, confined to the folder | `read-only` sandbox |
| `workspace` | `--restricted`: may also edit files in the folder, no shell | `workspace-write` sandbox |
| `full` | `--dangerously-skip-permissions` | no sandbox, no approvals |

Below `full`, Claude Code runs with `--restricted --strict-mcp-config --permission-prompts
none`: file tools stay inside the working folder, no settings files (so no hooks planted in
the folder) and no MCP servers load, and anything that would ask is denied. Codex runs with
`--ignore-user-config` and plugins, apps and computer use disabled, because MCP servers and
plugins run outside its sandbox; the model and effort from `config.toml` are passed on. Codex's
`read-only` sandbox still lets its shell read any file the user can read, so treat `read` on
Codex as "no writes", not "no reading".

Per agent, `default_permission` applies when a request asks for nothing. `max_permission` is
the ceiling inside the folders listed in `workdirs`; anywhere else the ceiling is `read`.
`default_workdir` (default `~/.a2a-workspace`) is used when a request names no folder.
Folders holding agent or system configuration (`~/.a2a`, `~/.claude`, `~/.codex`,
`~/.gemini`, `~/.config`, `~/.ssh`, `~/AppData`, PowerShell profiles), any folder that
contains one (such as the home folder), and network paths are never used below `full`; the
conversation keeps its current folder instead. A request asks for a level and folder with
metadata (`permission`, `workdir`) or with leading lines:

```text
[permission: workspace]
[workdir: C:\Projects\site]
Fix the broken links in the docs folder.
```

It gets the lower of what it asked and what that folder allows, and the reply says when it
was lowered. The owner raises the ceiling for a while from the PC's own terminal; a folder
grant applies only to that folder and only up to its level:

```bash
a2a grant claude workspace --dir C:\Projects\site --minutes 60
a2a grant codex full --minutes 15
a2a grants
a2a revoke            # all, or: a2a revoke codex
```

Grants are local files. Any program running as the same user could write them too, so keep
`max_permission` at the level you are comfortable leaving on.

Optional: `server.dispatch_log` appends one JSON line per new child session
(`by`, `child`, `harness`, `at`).

## Security notes

- The inbound server listens on `127.0.0.1` and refuses other hosts unless tokens are set.
- With tokens set, every request needs `Authorization: Bearer <token>`; the token's name is
  the peer identity shown to the local agent.
- Inbound text is framed as a message from a peer, with the granted level and folder.
- Requests are capped at 1 MB, sockets time out after 30 seconds, and malformed JSON-RPC
  gets an error reply.

## Tests

```bash
python -m unittest discover -s tests
```

The tests use a built-in `echo` runner, so they need no agent installed.

## License

MIT
