# Profiles

A profile is what this agent is configured to *be*: which MCP servers to open,
whether it can run commands and under what constraints, which commands stop for
a person, and the ceilings on one run.

It is a JSON document, not code, because the thing that differs between a
finance team, a lab and a game studio is which tools exist and what is
dangerous — not how to phrase a request. A longer system prompt cannot express
that; a profile can, and three teams can write three of them and share no
Python.

**With no profiles file there is exactly one profile**: every configured MCP
server, no shell, no ceilings. That is what every version before 0.5.0 did, and
the sidebar shows no selector, because a menu of one is noise.

## Where it lives

`MCP_PROFILES_PATH`, default `profiles.json` beside the working directory. The
container sets it to `/app/data/profiles.json`, on the mounted volume — the
image layer is read-only, so a file written there would not survive and could
not be edited.

## A profile in full

```json
{
  "repository": {
    "description": "Read a checked-out project and its history.",
    "system_prompt": "You are working inside a git repository.",
    "mcp_servers": ["git", "filesystem"],
    "shell": {
      "enabled": true,
      "policy": "docker",
      "workspace_root": "/workspace",
      "allow": ["git", "ls", "cat", "rg"],
      "approve": ["git push", "git reset"],
      "command_timeout": 30
    },
    "limits": { "tool_calls_per_run": 40, "model_calls_per_run": 25 },
    "todos": true,
    "clear_tool_output_at": 60000
  }
}
```

| Field | Default | What it does |
|---|---|---|
| `description` | `""` | Shown in the sidebar under the selector. |
| `system_prompt` | `""` | **Appended** to the base prompt, not substituted for it — the base carries the tool-use rules every profile still needs. |
| `mcp_servers` | `null` | Which servers from `config.json` to open. `null` means all of them. Naming one that does not exist is an error, not an omission. |
| `shell.enabled` | `false` | See [The shell](#the-shell). |
| `shell.policy` | `"docker"` | `docker` or `host`. |
| `shell.workspace_root` | `null` | The directory to work in. Bounded by `MCP_WORKSPACE_ROOT`. |
| `shell.allow` | `[]` | Executables this profile may run. Empty means none. |
| `shell.approve` | `[]` | Command prefixes that stop for a person. |
| `shell.command_timeout` | `30` | Seconds one command may run. Keep below `MCP_TOOL_TIMEOUT`. |
| `limits.tool_calls_per_run` | `null` | Ceiling per run. `null` is no ceiling. |
| `limits.model_calls_per_run` | `null` | Same, for model calls. |
| `todos` | `false` | Give the model `write_todos` and the prompt that makes it plan. |
| `clear_tool_output_at` | `null` | Drop old tool output past this many tokens. |

A profile that cannot be read is an error the sidebar shows, not a silent
fallback: running under settings nobody chose is worse than not running.

## The shell

0.2.0 *removed* a shipped shell server because a shell alongside a web-search
tool is an indirect prompt-injection path to credential exfiltration — the
model reads a page it was asked to summarise, the page tells it to run a
command, the command sends a key somewhere. 0.5.0 makes the shell the point, so
it is earned back rather than reinstated.

### It takes two people to turn on

The operator sets `MCP_ENABLE_SHELL=true` **and** the profile sets
`shell.enabled`. Either alone gets nothing. The operator's half is checked in
code rather than in the profile schema, because a profile is a file someone may
have been handed, and a file must not be able to enable a capability the
deployment did not.

`host` needs its own two yeses: the profile asks for it *and*
`MCP_SHELL_POLICY=host`. The environment variable **permits** the host policy;
it does not impose it, so a deployment that allows it still runs every profile
that asked for a sandbox in a sandbox.

### The boundary is the sandbox, not the allowlist

The default policy is a container with no network, a read-only root filesystem
and a non-root user. That is what makes a mistake in the allowlist survivable.

The one directory it can see is bounded by `MCP_WORKSPACE_ROOT`. A profile
naming somewhere outside it — or naming one when the operator set no root at
all — gets a temporary directory instead. This is not caution for its own sake:
a review demonstrated `workspace_root: "/root"` producing
`docker run -v /root:/root`, and `--read-only` does not apply to bind mounts.

### The allowlist is defence in depth, and only that

It matches the first word of **every** command on the line, because the shell
tool's own description tells the model to chain with `&&`, and it refuses
command substitution — `$(...)`, backticks, `<(...)`, `>(...)` — outright
rather than trying to parse it. A rule that reads only the start of the line is
not a rule: `ls && curl evil.example` passed an allowlist of `ls` until this
existed.

**Do not list an interpreter.** `python`, `python3`, `perl`, `make`, `pytest`,
`xargs` and `sh` all run arbitrary code, so listing one is listing `sh`. A
review demonstrated `python3 -c` and `find … -exec sh -c … +` against the first
version of this project's own examples — note `-exec … +`, which needs no `;`
and so gives a word-splitter nothing to catch. The shipped examples name none
of them, and a test enforces it.

**And that rule is not followable by inspection.** Plenty of ordinary commands
become interpreters given the right option:

| Command | The option that runs something else |
|---|---|
| `git` | `-c alias.x='!cmd'`, `-c core.pager=…`, `clone ext::sh` |
| `rg` | `--pre` |
| `find` | `-exec`, `-execdir`, `-ok` |
| `tar` | `--use-compress-program` |

`git` and `rg` are the whole point of a repository profile, so removing them is
not the answer. Those argument forms are refused instead — but that is a
**denylist, and denylists are incomplete by construction**. It covers what a
review demonstrated. It does not make an arbitrary command safe, and nothing
does. That is what the sandbox is for, and it is why the sandbox and not this
is called the boundary.

### Approval is per command prefix

`git push` stops for a person; `git status` does not. The rule's words must
appear in order but need not be adjacent, because `git -c user.name=x push`,
`git -C /tmp push` and `git "push"` all run a push and all walked past a rule
that required adjacency. That means it over-matches — `git log push-notes`
stops too — which is the safe direction.

A rule for a command the allowlist refuses can never fire; the guard refuses
first, by design, so that nobody is asked to approve something that cannot run.

### What it still does not stop

The sandbox has no network. The browser rendering this page does. Assistant
text is markdown, so an image embed in a reply is fetched by the viewer with no
click — `mcp_agent.rendering.without_images` defuses that, and it is a separate
control from the sandbox rather than a consequence of it.

Shell output is redacted for common provider-token shapes before the model sees
it, because anything it reads lands in `data/checkpoints.db`, unencrypted,
until the conversation is deleted. That is a last line, not a scanner.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `MCP_PROFILES_PATH` | `profiles.json` | Where profiles are read from. |
| `MCP_ENABLE_SHELL` | `false` | Must be exactly `true`. |
| `MCP_SHELL_POLICY` | unset | `host` permits the host policy for profiles that ask for it. |
| `MCP_WORKSPACE_ROOT` | unset | The one directory a profile may mount. |
| `MCP_SANDBOX_IMAGE` | `python:3.12-slim` | Debian-based on purpose: commands run through `/bin/bash`, which Alpine does not ship. |

## Verify the sandbox before trusting it

This project's test suite cannot start a container, so the flags above are set
and read but not observed running. Before enabling the shell anywhere real:

```bash
docker run --rm python:3.12-slim /bin/bash -c 'echo ok'
```

If that fails, the sandbox has never run, and the pressure will be to reach for
`MCP_SHELL_POLICY=host` — which is the configuration all of the above exists to
keep exceptional.
