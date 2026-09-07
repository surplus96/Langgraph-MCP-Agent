# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

---

## [0.4.1] — 2026-09-07

Five runtime defects, found by reviewing 0.4.0 against the plan for the next
version and then by mutating the fixes. The first had broken every turn since
0.3.0; the last two were introduced by the fixes above them and caught before
release.

### Fixed

- **Streamed output never reached the page.** `run_sync` marshalled the whole
  turn to the background loop, renderer included, so every `st.markdown` ran on
  a thread with no `ScriptRunContext` and Streamlit raised `NoSessionContext`.
  `run_query` caught it like any other failure, and that exception carries no
  message, so a turn died at its first chunk showing `Error during query
  processing: ` with nothing after the colon. Events now cross the thread
  boundary as data (`mcp_agent/turns.py`) and `app.py` draws them on the script
  thread. Introduced in 0.3.0 when the background loop replaced `nest_asyncio`.
- **A tool call running at the turn deadline made the conversation unusable.**
  The model node was checkpointed with its `tool_calls` and no `ToolMessage`
  followed — a sequence Anthropic rejects, rebuilt by every later turn on that
  thread, escapable only by resetting. Tool calls are now bounded individually
  (`MCP_TOOL_TIMEOUT`, default 60s) and a timeout becomes an ordinary tool
  error the model can read.
- **Two servers exposing the same tool name silently lost one.** Verified
  against `create_agent`: three tools in, two bound, and the sidebar still
  counted three. Tool names are namespaced by server, and a collision that
  survives that is reported rather than dropped.
- **The server prefix could produce a tool name the API rejects.** The first
  cut of the namespacing above pasted the config key straight on. Anthropic
  requires `^[a-zA-Z0-9_-]{1,64}$` and rejects the whole *request* when a name
  fails it, so a Smithery-style key — `@smithery-ai/server-sequential-thinking`,
  which `README.md` tells users to paste — would have failed every turn rather
  than one tool. `namespaced()` now cleans the prefix and shortens it to fit,
  keeping the tool's own name whole. Never released.
- **`Turn`'s deadline was dead code.** It was consulted only when collecting
  the result, which `app.py` reaches only after iteration has already ended, so
  the guarantee it documented — that a stuck loop cannot hold the browser —
  did not hold. Measured, not read: a stub that blocks the loop thread hung the
  turn indefinitely. Iteration now consults it, and the collect waits out only
  what is left of it. Never released.
- **A turn that produced nothing was reported as success**, appending an empty
  assistant bubble. The changelog has claimed since 0.3.0 that this was fixed;
  it was not.

### Added

- `tests/test_turns.py` — the first tests in this repository that drive a real
  chat turn through the real script. Their absence is why a streaming path that
  raised on every write shipped and stayed shipped.

  A mutation pass over the first version of that file found 18 of 24 mutations
  surviving, all of them in the streaming path the release is named after: the
  turn ends in `st.rerun()`, so every assertion read a page rebuilt from the
  transcript rather than from the events. Deleting the streaming loop from
  `app.py` left the whole suite green. The file now also drives `Turn`
  directly, and the two mutations that mattered most — the deleted loop, and
  a `thread_id` hard-wired so every conversation shares one checkpoint — are
  both red.

---

## [0.4.0] — 2026-09-07

Conversations now outlive the process, and the secret scan CI never actually
ran is now running and cannot be switched off from the branch it scans.

### Added

- **Conversations survive a restart.** `InMemorySaver` is replaced by SQLite
  (`CHECKPOINT_DB_PATH`, default `data/checkpoints.db` — the volume that already
  holds `config.json`). Two things had to come with it or the feature would only
  have looked real: the thread id now lives in the URL, because a checkpoint
  keyed by a UUID generated fresh on every browser session is written and then
  never asked for again; and the transcript is re-read on load, because the
  checkpointer restores what the *model* remembers while the messages the *user*
  sees are Streamlit session state and are gone. A side effect worth having:
  each browser tab gets its own conversation, and a conversation can be
  bookmarked. An unwritable path falls back to memory with a warning rather than
  failing the app.
- **Reset Conversation now deletes the conversation it abandons.** Rotating the
  thread id and walking away cost nothing while the store was in memory. Against
  a durable one it leaves rows the application offers no way to reach or remove.
  The button's tooltip says the deletion is permanent, since it also destroys a
  bookmarked link. Tidying up can never raise — a failure there must not leave
  someone stuck in the conversation they asked to leave.
- `docs/ARCHITECTURE.md`, `docs/MCP_TOOLS.md`, `CONTRIBUTING.md`, `SECURITY.md`,
  `CLAUDE.md` and this changelog.
- A `secrets` job in CI running gitleaks over the full commit history, pinned by
  version and checksum. gitleaks was previously listed in
  `.pre-commit-config.yaml` only, which CI never invokes — so no automated
  secret scan actually ran on the repository.
- Guards making that scan non-optional from inside the branch it scans. Three
  bypasses were reproduced against this repository and closed: an in-tree
  `.gitleaks.toml` with an `.*` allowlist, a `# gitleaks:allow` comment in the
  same commit as the secret, and appending a finding's own fingerprint to
  `.gitleaksignore`. CI additionally fails if `config.json` is ever tracked —
  its shape (a key as a bare JSON array element) defeats the default rules.
- `permissions: contents: read` on the CI workflow. Every job executes
  repository-controlled code and none of them write anything back.
- The sidebar now names any MCP server that failed to start or stopped
  responding. The pool had recorded these all along and nothing displayed them,
  so the only symptom was a tool count quietly lower than expected.
- `pre-commit` added to the dev dependency group. `CONTRIBUTING.md` told
  contributors to run `uv run pre-commit install`, which could not work.
- The suite grew from 125 tests to 150.

### Fixed

- **Editing `config.json` by hand no longer loses the edit.** The file is read
  from disk once per browser session, and **Apply Settings** wrote that copy
  back unconditionally — so a hand edit was silently overwritten and no tool was
  registered. With the in-app editor off (the default) this is the *only*
  documented way to add a tool. Applying settings now re-reads the file unless
  the in-app editor changed something in this session.
- **The container healthcheck can now pass.** It probed `/healthz`, which
  Streamlit does not serve; the correct path is `/_stcore/health`. As written,
  every container reported `unhealthy` for its whole life.

---

## [0.3.0] — 2026-09-07

A modernization of a codebase that, as inherited, did not start. Effectively a
rewrite of everything below `app.py`, with the UI behaviour preserved.

### Fixed — the app now runs

- Three independent blockers that each prevented startup.
- `nest_asyncio` plus a per-session event loop replaced by one process-wide
  background loop (`runtime.py`). The old loops were never closed, their
  lifetime did not match `set_event_loop`'s per-thread scope, and re-entrant
  loop patching corrupted the anyio cancel scopes the MCP stdio transport
  relies on.
- `thread_id` moved under `configurable` in the `RunnableConfig`, where it
  belongs; passing it at the top level only worked through a fallback.
- The streaming parser now reads every content block of every chunk. It
  previously read `content[0]` only, silently dropping parallel tool calls and
  interleaved thinking blocks.
- A run producing zero chunks is no longer reported as success. `QueryResult`
  has one explicit success/failure signal instead of an overloaded dict.
- The turn timeout is applied inside `run_query`, so a turn cut short still
  reports the text it streamed and the tokens it spent.

### Security

- **Login gate fails closed.** With `USE_LOGIN=true` and either credential
  blank, the app refuses to render a login form. Previously Compose turned an
  unset variable into `""`, the code compared `"" == ""`, and anyone who
  clicked the button was authenticated. Credentials are now compared with
  `hmac.compare_digest`.
- **MCP command allowlist** (`MCP_ALLOWED_COMMANDS`). Registering a server
  spawns a subprocess, so an unconstrained `command` field was arbitrary code
  execution by anyone who could reach the UI.
- **In-app tool editor gated** behind `MCP_ALLOW_TOOL_EDIT`, off by default.
- **Default configuration emptied.** It previously registered a shell tool
  alongside a web-search tool — a complete indirect-prompt-injection chain to
  credential exfiltration — and referenced two server scripts absent from the
  repository.
- **`config.json` untracked**, and `MCP_CONFIG_PATH` added so a container can
  point it at a mounted volume. Tools added through the UI previously died with
  the container.
- **Leaked credentials revoked.** An OpenAI key and a LangSmith key committed to
  `dockers/.env.example` in the first commit (2025-07-08) were revoked on
  2025-09-04, along with a Smithery profile reset. No usage was recorded. See
  [SECURITY.md](SECURITY.md) for why the history is not rewritten.
- System prompt now states that tool output is evidence, not instructions.
- Container runs as UID 1000 with a read-only rootfs, all capabilities dropped,
  `no-new-privileges`, a pids limit and a memory limit, bound to loopback.
- Six known-vulnerable transitive dependencies cleared (protobuf, pyarrow,
  pygments, requests, urllib3, starlette). They had survived because plain `uv
  lock` does a minimal update and leaves the transitive tree stale.

### Changed — pipeline

- **LangChain 1.x / LangGraph 1.x.** `create_react_agent` replaced by
  `create_agent`, with `system_prompt=` and `middleware=`.
- **MCP sessions held open** instead of one per tool call. Measured: 610 ms →
  10.5 ms on a local Python server (58x), 770 ms → 2.4 ms on an `npx` server
  (319x). A twelve-call turn drops from 9.2 s of pure overhead to 0.03 s. Each
  session is owned end to end by one keeper task, because an anyio cancel scope
  must be exited by the task that entered it.
- **Dead sessions report themselves.** A held session does not announce its own
  death — the keeper is parked, not reading the stream — so killing a server
  left everything looking healthy until a tool call failed with an empty
  `ClosedResourceError`. Both defects were found by fault injection and are now
  caught, recorded once, and re-raised as a message that says what to do.
- **Anthropic prompt caching** with three breakpoints. The system prompt and
  every tool schema were previously re-sent at full price on every ReAct
  iteration — up to `recursion_limit` times per user turn.
- **Token accounting** (`usage.py`) with per-turn and cumulative counts, cache
  read/write split and hit rate, surfaced in the sidebar. Handles the per-TTL
  cache-write keys that langchain-anthropic substitutes for the generic one.
- **Caching-floor warning.** Anthropic silently ignores `cache_control` below a
  per-model minimum (512 / 1024 / 4096 tokens). This project's prefix with no
  tools registered is 400 tokens, below all three, so caching was a no-op on
  every model until tools were added. The sidebar now says so instead of
  showing an unexplained 0% hit rate.
- **Effort control** (`low`…`max`), shown only for models that accept it —
  Haiku 4.5 rejects `output_config` with a 400.
- **History summarization** at 80% of the context window, as a safety net
  against outright context-length failure. Late on purpose: summarizing
  invalidates the cached prefix.
- **Model registry** (`models.py`) as the single source of truth, replacing five
  duplicated and drifted model lists. Verified against `GET /v1/models` on
  2026-09-04; `scripts/check_models.py` re-checks it.
- `claude-fable-5-1` restricted by project policy. The reason is recorded in
  `RESTRICTED_MODELS`, the model is omitted from the selector, and two tests
  enforce it. The reason is not surfaced in the UI.
- Anthropic-only. The OpenAI model IDs could not be verified against first-party
  documentation, so they were removed rather than guessed at.

### Changed — structure

- Everything testable moved out of `app.py` into `src/mcp_agent/`. `app.py` is
  now presentation only.
- Typed session state (`state.py`) replacing fifteen loose `st.session_state`
  keys initialized across six sites under four guard idioms — one of which left
  `agent` and `history` undefined on some paths.
- `utils.py` removed: a duplicate `ainvoke_graph` with no callers, an unreachable
  streaming branch, and ANSI-coloured `print` fallbacks that could not execute.
- Docstrings and comments normalized to English.

### Added

- 125 tests, none requiring a network or an API key, including
  `AppTest`-driven smoke tests and lifecycle tests for the session pool.
- `dockers/Dockerfile` and a consolidated `docker-compose.yaml`. The compose
  file previously referenced a `build:` target that did not exist.
- CI (`.github/workflows/ci.yml`): lint, format, type check, tests, `pip-audit`,
  and a Docker build.
- `.pre-commit-config.yaml` with ruff, gitleaks and whitespace hooks.
- `examples/mcp_server_time.py`, a runnable MCP server for trying the app out.
- `scripts/check_models.py`, which verifies the registry against the live API.
- `LICENSE`. MIT was claimed in three places with no file, so as distributed the
  repository was unlicensed. Upstream attribution to
  [teddylee777/langgraph-mcp-agents](https://github.com/teddylee777/langgraph-mcp-agents)
  retained.
- `.claude/agents/`: nine reviewer, diagnostician and verifier agent definitions
  used to review this work.

### Fixed — documentation

- Both clone URLs (one had a typo, one `cd`'d to the wrong directory).
- `cp .env.example .env` (the file is `dockers/.env.example`).
- `claude-3-haiku-latest`, which does not exist.
- A `blob:` screenshot URL that could never render.
- The 8501-vs-8585 port confusion.
- Node.js/npm documented as the hard prerequisite it is — the shipped
  configuration launched everything through `npx`.
- `USE_LOGIN` / `USER_ID` / `USER_PASSWORD` documented; they were read by code
  and mentioned nowhere.
- The four LangSmith variables marked as SDK-consumed; no code in this project
  reads them.

### Known limitations

- Conversation history uses an in-memory checkpointer and does not survive a
  process restart. A durable checkpointer was evaluated and deliberately
  deferred; see [docs/UPGRADE_PLAN.md](docs/UPGRADE_PLAN.md).
- One shared credential pair, no rate limiting, no session expiry. This is a
  single-user or trusted-team tool.

---

## [0.2.0] and earlier

No changelog was kept. See the commit history from `5cd21de` (2025-07-08)
onward.

[Unreleased]: https://github.com/surplus96/Langgraph-MCP-Agent/compare/v0.4.1...main
[0.4.1]: https://github.com/surplus96/Langgraph-MCP-Agent/releases/tag/v0.4.1
[0.4.0]: https://github.com/surplus96/Langgraph-MCP-Agent/releases/tag/v0.4.0
[0.3.0]: https://github.com/surplus96/Langgraph-MCP-Agent/releases/tag/v0.3.0
