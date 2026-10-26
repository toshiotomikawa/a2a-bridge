# a2a-bridge

A small local bridge for the [A2A protocol](https://a2a-protocol.org) (v1.0, HTTP + JSON-RPC).
It works in both directions on one PC:

- **Outbound.** Coding agents such as Claude Code and Codex (or any script) send messages to
  an A2A agent, for example a [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a),
  and get replies in the background, keeping the conversation's context.
- **Inbound.** The A2A agent can call Claude Code, Codex, Antigravity (`agy`), OpenCode and
  Grok Build back. Each one is served on its own local port with its own Agent Card, and runs
  in non-interactive mode under permission limits set by the PC's owner, or, when the owner
  allows it, is handed the message in a window the owner already has open.

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

## Inbound: let the A2A agent call local coding agents

```bash
a2a serve            # claude on 127.0.0.1:9901, codex on 127.0.0.1:9902
```

Antigravity (9903), OpenCode (9904) and Grok Build (9905) are off by default; turn each on
in `~/.a2a/config.json` under `server.agents`, for example
`"opencode": {"enabled": true, "model": "<provider>/<model>"}`. `model` is optional for every
agent; without it the harness uses its own default.

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

| Level | Claude Code | Codex | Antigravity | OpenCode | Grok Build |
| --- | --- | --- | --- | --- | --- |
| `read` | read and search, in the folder | `read-only` sandbox | read in the folder | read and search, in the folder | read and search, in the folder |
| `workspace` | also edit in the folder, no shell | `workspace-write` sandbox | also edit in the folder, no commands | also edit in the folder, no shell | also edit in the folder, no shell |
| `full` | `--dangerously-skip-permissions` | no sandbox, no approvals | `--dangerously-skip-permissions` | `--auto` | `--always-approve`, own config |

Below `full`, Claude Code runs with `--restricted --strict-mcp-config --permission-prompts
none`: file tools stay inside the working folder, no settings files (so no hooks planted in
the folder) and no MCP servers load, and anything that would ask is denied. Codex runs with
`--ignore-user-config` and plugins, apps and computer use disabled, because MCP servers and
plugins run outside its sandbox; the model and effort from `config.toml` are passed on. Codex's
`read-only` sandbox still lets its shell read any file the user can read, so treat `read` on
Codex as "no writes", not "no reading".

The other three have no single switch for this, so below `full` each runs isolated:

- **Antigravity** runs with its home folder pointed at `~/.a2a/isolated/agy-<level>` (sign-in
  stays in the system keyring), so no user settings, plugins or MCP servers load. That home's
  settings keep it in the working folder and deny commands, MCP and URLs; `workspace` adds
  `--mode accept-edits`. Refused actions are listed in the reply.
- **OpenCode** gets an inline config (`OPENCODE_CONFIG_CONTENT`, which outranks the user's)
  that denies every tool except reading, searching and, for `workspace`, editing inside the
  folder; `.env` files and OpenCode's own config stay off limits. It runs `--standalone`,
  and the folder's own OpenCode config, agents and plugins (which run as code) and Claude Code
  compatibility files are not loaded.
- **Grok Build**'s permission modes do not keep writes inside the folder on Windows, and its
  sandbox is Linux and macOS only. Below `full` it runs from `~/.a2a/isolated/grok` (no user
  config, plugins, memory, or Claude, Cursor and Codex hooks, skills and MCP servers) with a
  `PreToolUse` hook, `grok_guard.py`, that denies every tool but reading and, for `workspace`,
  editing inside the folder, never into agent or git configuration (`.git`, `.grok`, `.claude`,
  `.opencode` and the like), `.env` files or `opencode.json`. The hook denies on any error of
  its own. Deny rules for the shell,
  MCP, web fetch and the protected folders back it up.

A change of level starts a new Antigravity, OpenCode or Grok Build conversation: Antigravity
and Grok Build keep each level's conversations in its own home, and an OpenCode session keeps
the permissions approved in it.

### Open windows

An agent can also have a `window` helper: a program that hands the message to a session the
owner already has open (a TUI, say) and, when it can, prints the reply. A request picks it
with `[mode: window]` and names the window with `[target: <id>]` (or metadata `mode` and
`target`); `default_mode` sets it for every request. A window keeps the permissions the owner
gave it, which the bridge cannot lower, so window mode needs `full`; below that the request
runs in the background and the reply says so. Later turns in the same conversation reuse the
window. The helper is set in `config.json`:

```json
"window": {
  "argv": ["python", "-I", "<path>/send-to-window.py", "{target}", "{id}", "{text}"],
  "reply": "text"
}
```

Placeholders: `{id}` (a request id; the message asks the window to end its reply with
`DONE <id>`, which is removed), `{text}`, `{prompt_file}` (the same text with an
`AGENT MESSAGE [<id>]` header, in a file), `{target}` and `{seconds}`. `reply` is `text`
(stdout), `json` (a `reply` key on the last line) or `none` (delivery only).

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
- Antigravity, OpenCode and Grok Build get the prompt as a command-line argument or file,
  never through a shell; a `.cmd` shim is refused (for OpenCode the real binary beside the
  npm shim is used).
- Requests are capped at 1 MB, sockets time out after 30 seconds, and malformed JSON-RPC
  gets an error reply.

## Tests

```bash
python -m unittest discover -s tests
```

The tests use a built-in `echo` runner, so they need no agent installed.

## License

MIT
