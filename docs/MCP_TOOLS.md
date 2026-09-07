# Configuring MCP tools

The reference for the configuration file: its format, every field, what is
validated, and what happens when a server misbehaves. The
[README](../README.md#configuring-mcp-tools) has the short version.

## Where the file lives

| Environment | Path |
|---|---|
| Source checkout | `config.json` in the repository root |
| Docker | `/app/data/config.json`, on the mounted `./data` volume |
| Anywhere | Whatever `MCP_CONFIG_PATH` names |

Under Docker the file is on a volume specifically so that tools added through
the UI survive container recreation. The path this replaced wrote into the image
layer, so every tool a user added was lost on the next `docker compose up`.

**The file is gitignored and must stay that way.** It holds credentials for the
servers it launches.

If the file does not exist, the application starts with **no servers
registered** — see [Why the default is empty](#why-the-default-is-empty).

## Format

A JSON object mapping a server name to its definition:

```json
{
  "time": {
    "command": "python",
    "args": ["./examples/mcp_server_time.py"],
    "transport": "stdio"
  },
  "memory": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-memory@2025.9.25"],
    "transport": "stdio"
  },
  "remote": {
    "url": "https://example.com/mcp",
    "transport": "streamable_http"
  }
}
```

The server name is yours to choose. It appears in error messages and prefixes
nothing else, so pick something you will recognise when it fails.

### Fields

| Field | Required | Notes |
|---|---|---|
| `command` | one of `command` / `url` | The executable. Checked against the allowlist. |
| `args` | with `command` | Must be an array. Required even if empty. |
| `url` | one of `command` / `url` | For a server you connect to rather than launch. |
| `transport` | no | Inferred: `stdio` with `command`, `sse` with `url`. |
| `env` | no | Extra environment variables for the subprocess. |

`transport` may be `stdio`, `sse` or `streamable_http`. Set it explicitly when
using `streamable_http`, since the inference for a `url` entry gives you `sse`.

Use `env` for a server's own credentials:

```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github@2025.4.8"],
    "env": { "GITHUB_TOKEN": "ghp_..." },
    "transport": "stdio"
  }
}
```

This is the reason the file is gitignored.

## Validation

Every entry is checked before anything is launched, and the first failure stops
the load with a message meant for you rather than for a log:

- The entry must be a JSON object.
- It must have a `command` or a `url`.
- With a `command`, `args` must be present and must be an array.
- The `command` must appear in `MCP_ALLOWED_COMMANDS`, which defaults to
  `npx,uvx,node,python,python3,docker`.

A file that exists but does not parse **raises** rather than falling back to the
default. Silently falling back is how one stray comma used to destroy a user's
entire server list on the next save.

Writes are atomic (`write` to a temp file, then `os.replace`), so a crash
mid-write cannot truncate the file.

## Adding a server

### By editing the file

The default, and the one to prefer. Edit `config.json`, then click **Apply
Settings** in the sidebar. The sidebar reports the number of tools discovered.

### Through the UI

Only if `MCP_ALLOW_TOOL_EDIT=true`. The editor is not rendered otherwise.
Registering a server launches a subprocess, so this is deliberately an opt-in:
turn it on only if you trust everyone who can reach the UI as much as you trust
yourself with a shell.

## Lifecycle

Sessions are **held open** for as long as the configuration is unchanged, rather
than being opened per tool call. That is a 58x–319x difference in per-call
latency (see [ARCHITECTURE.md](ARCHITECTURE.md#mcp-sessions)), and it changes
what you should expect operationally:

- **Clicking Apply Settings with an unchanged config is nearly free** (~0.01 ms).
  It reuses the open pool.
- **Changing the config closes every session and starts new ones.** The old
  server subprocesses are shut down first; they are not left running.
- **A server that fails to start is recorded and skipped.** One broken entry
  does not cost you every other tool. The sidebar names the server and the
  error.
- **A server that dies while held open is not noticed until a tool is called.**
  Nothing is reading its stream in between. When a call does fail, the model is
  told the server has stopped responding and that Apply Settings will reconnect
  it — rather than receiving the empty `ClosedResourceError` the transport
  actually raises.

Tool definitions are sorted by name, so the tool block stays byte-stable across
rebuilds. That is what lets the cached prompt prefix be reused.

## Security

Read [SECURITY.md](../SECURITY.md) before registering anything you did not
write. The two rules that matter most:

### Do not combine a fetching tool with a privileged tool

A web-search or web-fetch server returns attacker-controlled text. A filesystem
or shell server acts on the model's decisions. Put both in one agent and a page
the model reads can drive the shell — an exfiltration path that needs no access
to your UI at all.

The system prompt tells the model to treat tool output as evidence rather than
instructions. That is a mitigation, not a control. Do not rely on it.

### Pin versions

```json
"args": ["-y", "@modelcontextprotocol/server-memory@2025.9.25"]
```

not

```json
"args": ["-y", "@modelcontextprotocol/server-memory@latest"]
```

`npx` fetches and executes whatever is published at the moment it runs. `@latest`
means you are re-deciding to trust the publisher on every single startup.

Note also what the allowlist does *not* do: `npx` can fetch and run an arbitrary
package, and `python` can run an arbitrary script. `MCP_ALLOWED_COMMANDS` stops
`command: "bash"`; it does not police `args`. The real control is who is allowed
to edit this file.

## Why the default is empty

The default configuration this replaced registered a shell tool
(`desktop-commander`) alongside a web-search tool — exactly the combination
warned against above, shipped as the out-of-the-box experience. It also
referenced two server scripts that do not exist in this repository, so it could
not have worked as written.

`examples/mcp_server_time.py` is in the repository as something safe to start
with; `example_config.json` registers it.

## Finding servers

[Smithery](https://smithery.ai/) indexes public MCP servers, as does the
[official servers repository][official]. Both are worth reading the source of
before you register anything from them.

[official]: https://github.com/modelcontextprotocol/servers

## Troubleshooting

| Symptom | Cause |
|---|---|
| `command 'X' is not permitted` | Not in `MCP_ALLOWED_COMMANDS`. Add it deliberately, or use a permitted launcher. |
| `requires an 'args' field` | `command` without `args`. Use `[]` if there are none. |
| `is not valid JSON` | A trailing comma, usually. The file is not loaded at all until it parses. |
| Tool count is 0 after Apply Settings | Every server failed to start; the sidebar names them. |
| Server starts by hand but not here | Usually `PATH` or a missing credential. The subprocess inherits this process's environment plus `env`. |
| `no longer responding` mid-conversation | The server process died. Apply Settings reconnects. |
| Everything is slow, ~700 ms per call | Sessions are not being reused — the config is changing between calls. |
