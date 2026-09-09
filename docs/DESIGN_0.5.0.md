# 0.5.0 design — the operation agent

**Status: proposal. Nothing here is built.** This document exists to be
approved, rejected or amended before any code is written.

## What is being asked for

Today this project is a chat window over whatever MCP servers happen to be in
`config.json`. The next version is meant to be an **operation agent**: you say
what you want done in ordinary language, and it does it — across MCP tools *and*
the command line, with defaults good enough that a team in any industry can
point it at their own tools and have it work.

Two constraints come with that, and they pull against each other:

- **Generality.** Not a devops bot, not a data bot. The default configuration
  must not assume a toolchain, and adding one must not require editing Python.
- **The existing security posture.** 0.2.0 removed the shipped shell server
  (`desktop-commander`) precisely because a shell alongside a web-search tool is
  an indirect prompt-injection path to credential exfiltration. That reasoning
  does not stop being true because the shell is now the point. This version has
  to earn the capability back, not quietly reinstate it.

Everything below is shaped by that second constraint.

## What already exists

| Piece | State | Reused how |
|---|---|---|
| Long-lived MCP sessions (`sessions.py`) | Shipped, 0.3.0 | Unchanged. Tools become one capability among several. |
| Durable checkpointer (`checkpoints.py`) | Shipped, 0.4.0 | **Prerequisite** for approvals: `HumanInTheLoopMiddleware` resumes an interrupted graph from the thread. |
| Streaming across the thread boundary (`turns.py`) | Shipped, 0.4.1 | Extended with a third event kind (see *Events*). |
| Per-call tool timeout (`sessions.py`) | Shipped, 0.4.1 | Applies to shell calls too. |
| Prompt caching + summarization middleware | Shipped, 0.3.0 | Order matters once more middleware joins; see *Middleware order*. |
| Command allowlist for MCP servers (`config.py`) | Shipped, 0.2.0 | The model for the shell allowlist, not the same list. |

Nothing here needs rewriting. 0.5.0 is additive.

## What LangChain 1.4 already provides

Verified against the installed version by importing them, not from memory
(`langchain.agents.middleware`, `langchain_anthropic.middleware`):

| Component | What it gives us |
|---|---|
| `ShellToolMiddleware` | A `shell` tool with a persistent session, `workspace_root`, `startup_commands`, `redaction_rules`, and a pluggable execution policy. |
| `DockerExecutionPolicy` | `network_enabled=False` (default), `read_only_rootfs`, `user`, `memory_bytes`, `cpus`, `command_timeout`, `max_output_lines`. |
| `HostExecutionPolicy` | Same bounds, no isolation. |
| `CodexSandboxExecutionPolicy` | OS-level sandbox via the `codex` binary. |
| `HumanInTheLoopMiddleware` | `interrupt_on={tool: config}`; interrupts the graph, requires a checkpointer. |
| `ToolCallLimitMiddleware`, `ModelCallLimitMiddleware` | Per-run and per-thread ceilings. |
| `TodoListMiddleware` | A `write_todos` tool and the prompt that makes a model use it. |
| `ContextEditingMiddleware` (`ClearToolUsesEdit`) | Drops old tool output instead of re-sending it. |
| `ProviderToolSearchMiddleware`, `LLMToolSelectorMiddleware` | Two ways to stop sending every tool schema every turn. |
| `PIIMiddleware`, `RedactionRule` | Redaction on the way in and out. |
| `ModelFallbackMiddleware`, `ModelRetryMiddleware`, `ToolRetryMiddleware` | Failure handling. |

**The bulk of this version is configuration and interface, not new machinery.**
That is the main reason to believe the scope is achievable.

## Design

### 1. Capabilities, not tools

Introduce one concept: a **capability** is a source of tools plus the policy
that governs it. Three ship:

- `mcp` — the existing `SessionPool`. Unchanged.
- `shell` — `ShellToolMiddleware` under an execution policy.
- `builtin` — `write_todos`, and nothing else for now.

`agent.py` grows one function that assembles capabilities into a
`create_agent` call. Nothing else in the runtime learns about shells.

### 2. Profiles are data

A **profile** is a JSON document — the same file kind as `config.json`, and
loadable from the same volume:

```json
{
  "name": "repository",
  "description": "Work inside a checked-out project.",
  "system_prompt": "...",
  "mcp_servers": ["git", "filesystem"],
  "shell": {
    "enabled": true,
    "policy": "docker",
    "workspace_root": "/workspace",
    "allow": ["git", "ls", "cat", "rg", "pytest"],
    "approve": ["git push", "rm"]
  },
  "limits": { "tool_calls_per_run": 40, "model_calls_per_run": 25 }
}
```

This is what makes the thing industry-agnostic in a way a longer default prompt
never would: a finance team, a lab and a game studio write three profiles and
share zero Python. Ship four (`general`, `repository`, `analysis`, `research`)
as examples, not as the product.

**Open question for you:** JSON, or YAML with comments? JSON keeps one parser
and one editor in the sidebar; YAML is friendlier to hand-write. I lean JSON.

### 3. The shell is off until someone turns it on

Defaults, all of them deliberate:

| Setting | Default | Why |
|---|---|---|
| `MCP_ENABLE_SHELL` | `false` | A fresh install of a chat app must not be able to run commands. |
| policy | `docker` | `network_enabled=False` closes the exfiltration path that got the shell removed in 0.2.0. |
| `read_only_rootfs` | `true` | Writes go to the mounted workspace or nowhere. |
| `user` | `nobody` | Not root inside the container either. |
| `command_timeout` | 30s | Below `MCP_TOOL_TIMEOUT` (60s), for the same reason that one sits below the turn budget. |
| allowlist | profile-supplied, empty means deny | Same shape as `MCP_ALLOWED_COMMANDS`, and the same reasoning. |

`HostExecutionPolicy` is available and is **not** a supported default. It runs
the model's commands as the Streamlit process. Reaching it takes an explicit
`MCP_SHELL_POLICY=host`, and the sidebar says so in words.

### 4. Approval is the feature, not the friction

`HumanInTheLoopMiddleware` interrupts before a matching tool call. The graph
stops, the thread is checkpointed, and the page has to render a decision. This
is the one genuinely new piece of interface, and it is where the risk is:

- `Turn` gains a third event kind, `approval`, carrying the pending call.
- Iteration ends on it. The page renders the command and Approve / Reject.
- The decision resumes the graph on the same `thread_id` — which works only
  because 0.4.0 made checkpoints durable. On a browser reload mid-approval the
  pending call is still there.

Rejection has to reach the model as a tool result, or the message sequence
breaks in exactly the way 0.4.1 fixed for timeouts.

### 5. Many tools, one cached prefix

Ten MCP servers is a large tool block, and the tool block *is* the cached
prefix — the thing 0.3.0 spent its effort making byte-stable. Two levers exist
and they trade against each other:

- `ProviderToolSearchMiddleware` defers tool schemas to a provider-side search.
  Cheaper prefix, but the prefix changes shape, and the caching work assumed it
  does not.
- `LLMToolSelectorMiddleware` picks a subset per turn. Same problem, plus a
  model call.

**Proposal: neither by default.** Add a measured threshold later. This is
precisely the kind of unmeasured optimisation this repository has a rule
against, and the honest answer today is that nobody has measured a prefix with
ten servers on it.

### 6. Middleware order

`create_agent` applies middleware as a chain, so order is behaviour, not style:

1. `TodoListMiddleware` — plan first.
2. `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware` — the runaway stops.
3. `HumanInTheLoopMiddleware` — approval before execution.
4. `ShellToolMiddleware` — the capability itself.
5. `SummarizationMiddleware` — existing, late trigger.
6. `AnthropicPromptCachingMiddleware` — existing, last so it sees the final shape.

This ordering is a claim, and claims in this repository get tested. See below.

## What is deliberately not in 0.5.0

Listed because the failure mode this version invites is scope that grows one
reasonable step at a time:

- No multi-agent handoff or sub-agents.
- No scheduling, no unattended runs. A human is in the loop by construction.
- No credential manager. `env` in `config.json` stays the mechanism.
- No web UI beyond Streamlit.
- No writing back to MCP servers' own configuration.
- No auto-approve mode, not even behind a flag. If that is wanted, it is its
  own version with its own argument.

## How this gets verified

The repository's standard is that a test counts only if breaking the code
breaks it. Concretely, before 0.5.0 can be called done:

1. **The shell is off by default** — mutate the default to `true` and the suite
   must go red.
2. **A denied command is denied** — the allowlist rejection is asserted on
   behaviour, not on a log line.
3. **Rejection produces a tool result** — the same invalid-sequence failure
   0.4.1 fixed, reached from a different direction.
4. **An interrupt survives a reload** — a second checkpointer against the same
   path, as `test_checkpoints.py` already does.
5. **Middleware order is asserted** — the built chain's order, so a reorder
   during a refactor is loud.
6. **A profile with an unknown MCP server fails clearly**, not silently.
7. Prefix-token measurement before and after, since caching is the one thing
   here with a number attached.

## Rough shape of the work

| Step | Touches | Notes |
|---|---|---|
| 1. Profiles (load, validate, sidebar) | new `profiles.py`, `app.py` | No behaviour change; profiles that only name MCP servers reproduce today's app. |
| 2. Capability assembly | `agent.py` | Middleware chain becomes constructed rather than literal. |
| 3. Shell capability | `agent.py`, new `shell.py` | Off by default; Docker policy; allowlist. |
| 4. Approvals | `turns.py`, `app.py` | The riskiest step, and the one worth reviewing first. |
| 5. Limits, todos, context editing | `agent.py` | Configuration. |
| 6. Docs, both READMEs, changelog | | Documentation has caught three live defects in this repo; it is not the tail end. |

Steps 1–3 are independently shippable. Step 4 is where a design review is worth
most, because getting the interrupt/resume contract wrong is expensive later.

## What needs your decision

1. **Docker as the default shell policy.** It means an operator without a
   Docker socket gets no shell until they choose `host` explicitly. Correct, or
   too strict?
2. **Profiles as JSON** (one parser, editable in the sidebar) or YAML.
3. **Approval granularity** — per command, per command prefix, or per tool.
   Per-prefix (`git push`) is the most useful and the most fiddly.
4. **Whether step 4 ships in 0.5.0 at all**, or 0.5.0 is steps 1–3 and
   approvals are 0.6.0. Shipping a shell without approvals is not an option;
   shipping profiles without a shell is.
