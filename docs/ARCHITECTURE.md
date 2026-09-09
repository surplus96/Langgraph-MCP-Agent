# Architecture

How this application is put together, and why it is put together that way. If
you only want to run it, the [README](../README.md) is enough. Read this before
changing anything in `src/mcp_agent/`.

## The shape of the problem

Three things have to coexist that do not naturally want to:

- **Streamlit** re-runs the entire script top to bottom on every interaction, on
  a synchronous thread, with no native asyncio support.
- **MCP servers** are subprocesses talking a stream protocol over stdio, whose
  client sessions are async context managers guarding anyio cancel scopes.
- **LangGraph** streams asynchronously and holds conversation state across turns.

Most of the design below exists to reconcile those three.

## Layers

```
app.py                     Presentation only. Streamlit widgets, layout, copy.
  │                        Nothing here is testable without a script run.
  ▼
src/mcp_agent/             The application. No Streamlit import below this line
  │                        except in state.py, which is where sessions live.
  ├── agent.py             Build the ReAct agent; run one turn; parse the stream.
  ├── sessions.py          Long-lived MCP sessions, one per configured server.
  ├── models.py            The model registry and per-model capabilities.
  ├── config.py            Load, validate and persist the MCP server config.
  ├── usage.py             Token accounting.
  ├── checkpoints.py       Conversation state that outlives the process.
  ├── turns.py             One turn, run on the loop, drawn on the caller's thread.
  ├── rendering.py         Which pane a streamed event is drawn into, and with what.
  ├── streaming.py         Graph stream → callback.
  ├── runtime.py           The one background event loop.
  ├── auth.py              The login gate.
  └── state.py             Typed per-browser-session state.
```

The rule that keeps this honest: **`app.py` must contain nothing that can be
tested without a script run.** Anything testable in isolation belongs in the
package. What is left — the login gate, the editor gate, the caching-floor
warning, the failure banner — only exists at script level, and
`tests/test_app_smoke.py` drives it through Streamlit's `AppTest`.

`usage.py` is a leaf on purpose. `state.py` needs `TokenUsage`, and importing it
must not drag in `langchain`, `langgraph`, Streamlit or an event loop. It does
import `langchain_core.messages.ai.UsageMetadata` — types only, no runtime.

## The event loop

`runtime.py` owns exactly one asyncio loop for the whole process, on a daemon
thread, created on first use. Every async call from the Streamlit thread goes
through `run_sync()`, which marshals to that loop with
`asyncio.run_coroutine_threadsafe` and blocks.

The previous approach — `nest_asyncio.apply()` plus a fresh loop per browser
session stashed in `st.session_state` — failed three ways at once: the loops
were never closed, a per-session lifetime does not match `set_event_loop`'s
per-thread scope, and re-entrant loop patching corrupts the anyio cancel scopes
the MCP stdio transport depends on. One process-wide loop avoids all three.

The consequence to remember: **anything that touches an MCP session must run on
that loop.** `SessionPool` is documented as not thread-safe for exactly this
reason.

## MCP sessions

The adapter's default is one session per tool call. Measured on this project:

| | per-call session | held session | speedup |
|---|---|---|---|
| Local Python server | 610 ms | 10.5 ms | 58x |
| `npx` server (warm cache) | 770 ms | 2.4 ms | 319x |

Roughly 78% of the per-call cost is importing the server's Pydantic models into
a fresh interpreter, which repetition does not warm up. A ReAct turn making a
dozen tool calls pays that cost a dozen times — about 9 seconds on `npx` before
the model has said anything.

So `sessions.py` holds sessions open. The cost of that speed is lifecycle:

- A **per-call** session is self-healing. If a server dies, the next call starts
  a new one and nobody notices.
- A **held** session is not. Death has to be noticed, reported, and recovered
  from. That is what most of `sessions.py` is.

### The keeper-task pattern

An anyio cancel scope must be exited by the task that entered it. Entering
`async with client.session(name)` in one `run_sync` call and exiting it in
another raises `RuntimeError: Attempted to exit cancel scope in a different
task` — and because every tool call in between succeeds, the failure surfaces
only at teardown, leaking the server subprocess.

So each session is owned end to end by one long-lived **keeper task**, which:

1. enters the session,
2. loads its tools and publishes them through a future,
3. waits on a `stop` event and does nothing else,
4. exits the session in the same task when told to stop.

`aclose()` **signals** rather than cancels, for the same reason: a cancellation
delivered from another task unwinds the scope from the wrong place.

### Failure reporting

A held session does not announce its own death. The keeper is parked on
`stop.wait()`, not reading the stream, so killing a server process leaves
everything looking healthy until something calls a tool — and the raw failure
that then reaches the model is `ClosedResourceError` with an empty message.
Both defects were found by killing a real server, not by reading the code.

`_guard()` also bounds each call (`MCP_TOOL_TIMEOUT`, default 60s). `run_query`
bounds the *turn*; if that deadline lands mid-call, the model node is
checkpointed with `tool_calls` and no `ToolMessage` follows — a sequence
Anthropic rejects, rebuilt by every later turn on that thread. Bounding the
call keeps the pair complete, because `ToolException` becomes an ordinary tool
result. Set the value below the turn budget or the original failure returns.

Tool names are namespaced by server. Without that, two servers exposing
`search` collide and `create_agent` binds only one — verified: three tools in,
two bound, with the sidebar still reporting three.

The prefix is built by `namespaced()` rather than by the adapter's
`tool_name_prefix`, which pastes the config key on unchecked. Anthropic rejects
a *request* whose tool names do not match `^[a-zA-Z0-9_-]{1,64}$`, and the key
is whatever the user pasted — Smithery's own snippets use
`@smithery-ai/server-sequential-thinking` — so one such entry would cost every
turn rather than one tool. `namespaced()` cleans the prefix and drops as much
of it as it must to fit, keeping the tool's own name whole because that is what
the model reasons about. Only the LangChain-side name changes; the adapter
closes over the MCP tool's real name for the call itself.

`_guard()` wraps every async tool at the one place the failure is observable
(a tool with no `coroutine` is returned untouched rather than half-wrapped). It
distinguishes transport death from an ordinary tool error by walking the `raise
from` chain against a set of exception names, records the failure once, and
re-raises a `ToolException` that says what actually happened and what to do
about it.

### Pool identity

`open_pool(config)` reuses the open pool when the config is unchanged (~0.01 ms)
and closes the old one before building a new one otherwise. Merely dropping the
old pool would leave its server subprocesses running.

## The agent

`build_agent()` calls LangChain's `create_agent` with two middlewares, in this
order:

1. **`SummarizationMiddleware`** at 80% of the context window. A safety net, not
   a routine saving — with a 1M-token window a chat session realistically never
   reaches it, but without it a long one fails outright on a context-length
   error instead of degrading. It triggers late because summarizing rewrites
   history and therefore throws away the cached prefix.
2. **`AnthropicPromptCachingMiddleware`**, which sets three cache breakpoints:
   the system prompt's last block, the last tool definition (one trailing
   breakpoint covers the whole contiguous tool block), and a top-level one that
   follows the growing message tail.

Without caching, the system prompt and every tool schema are re-sent at full
price on **every ReAct iteration** — up to `recursion_limit` times per user
turn, not once.

### The floor that makes caching a no-op

Anthropic silently ignores `cache_control` on a prefix shorter than a
per-model minimum: 512 tokens for Opus 5, 1024 for Sonnet 5, 4096 for Haiku
4.5. This project's prefix with no MCP tools registered is **400 tokens** by
the estimator's own reckoning (`len(SYSTEM_PROMPT) // 4`, measured: 1602
characters), which is below every one of those floors — so with no tools
registered, caching does nothing on **any** of the three models. Opus 5 clears
its floor after the first couple of tool schemas; Sonnet 5 and Haiku 4.5 need
substantially more.

Nothing in the API reports this. `AgentBundle.estimated_prefix_tokens` and
`ModelSpec.min_cacheable_tokens` exist so the sidebar can say "your prefix is
below this model's floor" instead of showing an unexplained 0% hit rate. The
estimate is deliberately conservative — an exact count needs the
`count_tokens` endpoint, and a warning is cheaper than a silent no-op.

### Streaming across the thread boundary

The agent runs on the background loop; Streamlit only allows drawing from its
script thread. Marshalling the whole turn to the loop — renderer included —
means every `st.markdown` runs where Streamlit raises `NoSessionContext`, and
`run_query` catches it like any other failure. That exception carries no
message, so the symptom was a turn dying at its first chunk with an error whose
reason was blank. It shipped in 0.3.0 and survived until a test finally drove a
chat turn.

`turns.py` moves events across as data: `Turn` runs the coroutine on the loop
and hands `TurnEvent`s to whichever thread iterates it. Attaching a script-run
context to the loop thread is the tempting one-liner and is worse — that thread
is process-wide, so writes would land in whichever browser session attached
last.

Queued events are coalesced to the last of each kind. Each carries the full
accumulated text or tool log rather than a delta, so the earlier ones are
redundant.

Iteration ends when the turn finishes **or** when the deadline
(`timeout_seconds` plus a 30s grace) passes, and the deadline is checked
*before* the wait as well as after it. Both matter, and each was found by
mutation rather than by reading: the script thread sits inside that loop, so
ending only on the future means a loop that stops answering holds the browser
indefinitely — and checking only when the event queue runs dry leaves a stream
that keeps producing unbounded, measured at 3.05s of events against a 0.10s
deadline.

The grace is what makes the two deadlines ordered rather than simultaneous.
`Turn`'s clock starts at construction; `run_query`'s starts when the loop gets
round to it, and a busy loop puts the whole gap between them. Without the
grace, `Turn` wins that race and the page gets "did not respond" instead of the
partial text and spent tokens `run_query` owns its timeout to preserve.

### Streaming and the timeout

`StreamAccumulator` parses **every content block of every chunk**. The previous
implementation read only `content[0]`, so parallel tool calls and interleaved
thinking blocks were dropped silently.

`run_query()` owns its own `asyncio.timeout`. This is deliberate: a turn cut
short still reports the text it streamed and the tokens it already spent.
Cancelling from the caller would strand both inside this frame.

`QueryResult.error` is the single success/failure signal. The code this
replaced overloaded a plain dict, so a run producing zero chunks looked like a
success and appended an empty assistant turn to the history.

## Token accounting

`usage.py` is four integers and some arithmetic, with two subtleties worth
knowing:

- **`input_tokens` already includes cached tokens.** Anthropic reports them
  separately and langchain-anthropic folds them back in, so
  `cache_read / input_tokens` is the share served from cache. It is a share of
  *tokens*, not of cost — cache reads still bill at roughly a tenth of the
  normal rate.
- **Cache writes may arrive per-TTL.** When Anthropic returns a breakdown,
  langchain-anthropic zeroes `input_token_details["cache_creation"]` and moves
  the counts to `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`, so as
  not to double count. Reading only the generic key loses the entire
  cache-write figure. The streaming path does not hit this today
  (`MessageDeltaUsage` carries no breakdown) but any non-streaming call would,
  and a 1h or mixed TTL makes it routine.

Every field is coerced through `_as_int`, which logs and drops anything
unexpected. Accounting must never be able to fail a turn.

## The model registry

`models.py` is the single source of truth, replacing five duplicated model lists
that had drifted apart. Each `ModelSpec` carries what the code actually needs to
branch on:

- `max_tokens`, `context_window` — request shaping and the summarization trigger.
- `min_cacheable_tokens` — the caching floor above.
- `supports_effort`, `supports_adaptive_thinking` — Haiku 4.5 rejects
  `output_config={"effort": ...}` with a 400, so `build_model` withholds it
  rather than sending it and hoping. The UI hides the control for the same
  reason.

`build_model` never sends `temperature`; on reasoning models it conflicts with
the effort and thinking settings.

`RESTRICTED_MODELS` is a policy list, not a capability list: entries are real,
usable models that this project has decided not to spend on without a sign-off.
The registry records why, `available_models()` omits them from the selector, and
a test fails if an id appears in both maps. The reason itself is not surfaced in
the UI — it is a note for whoever is deciding, not for whoever is chatting.

## Configuration and trust boundaries

`config.json` describes MCP servers to launch. **Registering a server spawns a
subprocess**, so an unconstrained `command` field is arbitrary code execution by
whoever can reach the UI. Two gates:

- `MCP_ALLOWED_COMMANDS` — an allowlist, defaulting to
  `npx,uvx,node,python,python3,docker`.
- `MCP_ALLOW_TOOL_EDIT` — off by default; the in-app editor is not rendered
  unless it is on.

`DEFAULT_CONFIG` is empty on purpose. The default it replaced registered a shell
tool alongside a web-search tool, which is a direct indirect-prompt-injection
path to credential exfiltration, and referenced two server scripts that do not
exist in this repository.

The system prompt states that tool output is evidence, not instructions. That is
mitigation, not a control — see [SECURITY.md](../SECURITY.md).

`config.py` imports no Streamlit and raises rather than rendering. The UI layer
decides how to present a failure.

## Session state

`state.py` replaces fifteen loose `st.session_state` keys initialized across six
sites under four different guard idioms — one of which keyed seven unrelated
fields off `session_initialized`, so any path that set that flag without running
the block left `agent` and `history` undefined.

One dataclass, one key, one accessor. `reset_conversation()` changes the
`thread_id` as well as clearing the transcript, so the checkpointer's history is
abandoned along with the display rather than the two diverging.

### Conversation state that survives a restart

`checkpoints.py` stores conversations in SQLite, on the same volume as
`config.json`. Three pieces have to hold together, and the first one alone is
the part that looks like the feature:

1. **The checkpointer is durable.** SQLite via `langgraph-checkpoint-sqlite`,
   opened on the background loop because the `aiosqlite` connection belongs to
   whichever loop created it. An unwritable path falls back to memory with a
   warning: losing persistence is a degradation, losing the app is not.
2. **The thread id survives.** It was a UUID generated per browser session, so
   every checkpoint was written under a key nothing would ever ask for again.
   It now lives in the URL's query string, which survives a reload, can be
   bookmarked, and gives each tab its own conversation for free.
3. **The transcript is re-read.** The checkpointer restores what the *model*
   remembers. What the *user* sees is `AppState.history`, which is Streamlit
   session state and is gone. Restoring one without the other returns a blank
   page in front of an agent that silently recalls everything.

Set `CHECKPOINT_DB_PATH=:memory:` to opt out deliberately rather than by
accident.

**Resetting deletes.** Rotating the thread id and walking away was fine while
the store was in memory and the process was about to forget it anyway. Against
a durable store it leaves rows the application then offers no way to reach or
remove — a file that only grows, holding conversations the user believes they
discarded. So the reset button erases the thread it abandons, and says so in
its tooltip, because that also destroys a bookmarked link to it. The deletion
happens *after* the id rotates and can never raise: failing to tidy up must not
leave someone stuck in the conversation they asked to leave.

`load_history` reconstructs text turns only. Tool calls are in the checkpoint,
but the per-turn log the UI shows is assembled from the stream, and inventing a
different-looking one would be worse than showing none.

## What a turn looks like

```
user submits a prompt in app.py
  └─ Turn(agent, query, ...)                      turns.py
       └─ run_coroutine_threadsafe(run_query(…))  → background loop
            └─ astream_graph(agent, ...)          streaming.py
                 └─ create_agent graph
                      ├─ SummarizationMiddleware  (usually a no-op)
                      ├─ AnthropicPromptCachingMiddleware
                      ├─ model call               → Anthropic
                      └─ tool call                → held MCP session (sessions.py)
            └─ StreamAccumulator                  text, tool log, usage
       └─ TurnEvent queue → iterated on the script thread
            └─ draw(event, ...)                   rendering.py
  └─ turn.result → QueryResult → history, sidebar totals
```

The first line of that used to read `run_sync(run_query(...), timeout)`, which
is the bug 0.4.1 fixed: it marshals the renderer to the loop along with
everything else, and Streamlit refuses to draw from there. The two-column shape
above is the point — everything indented under the loop runs on it, and only
what comes back through the queue is drawn.

## Testing

443 tests, none of which need a network or an API key. The parts that matter:

- **`test_caching.py`** intercepts the middleware's own public hook, so "caching
  is wired up" is a checked claim rather than an assumption. Whether the cache
  is *hit* still needs a live call; the sidebar reports that.
- **`test_sessions.py`** covers the lifecycle, including a killed server.
- **`test_app_smoke.py`** runs the actual script through `AppTest`.
- **`test_version.py`** fails when `__version__`, `pyproject.toml` and the
  Compose image tag disagree. They had drifted two releases apart, silently,
  because nothing imports `__version__`.
- **`test_rendering.py`** covers `draw`, which is why `draw` is here rather than
  in `app.py`. Anything driving the script sees the page *after* `st.rerun()`,
  rebuilt from the transcript, so every branch of it was free: tool output could
  go through `st.markdown` — the markdown escape it exists to prevent — with the
  whole suite green.

The standard applied here is **mutation**: a test suite is only trusted once
breaking the code has been shown to break the suite. Deleting the caching
middleware once left all 77 tests green; that is what the wiring tests exist to
prevent. See [CONTRIBUTING.md](../CONTRIBUTING.md).
