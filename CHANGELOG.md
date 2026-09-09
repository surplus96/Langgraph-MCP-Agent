# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

---

## [0.5.0] — 2026-09-09

The chat window becomes an operation agent: it can run commands, and what it is
allowed to do is written down as data rather than compiled in. The design and
the reasoning behind it are in [docs/DESIGN_0.5.0.md](docs/DESIGN_0.5.0.md);
[docs/PROFILES.md](docs/PROFILES.md) is the reference.

### Added

- **Profiles.** A JSON document naming which MCP servers to open, whether a
  shell exists and under what constraints, which commands stop for a person,
  and the ceilings on one run. This is what makes the agent usable outside the
  toolchain it was built against without editing Python: three teams write
  three profiles and share no code. **With no profiles file nothing changes** —
  there is exactly one profile, it opens every configured server and has no
  shell, and the sidebar shows no selector.
- **A shell capability, off until two separate switches say yes.** One is
  operator-side and one is in the profile; the same person may hold both, but
  neither file can turn it on alone. The operator sets
  `MCP_ENABLE_SHELL=true` and the profile sets `shell.enabled`; either alone
  builds nothing. Commands run in a container with no network, a read-only root
  and a non-root user, in a directory bounded by `MCP_WORKSPACE_ROOT`.
  `HostExecutionPolicy` is reachable and never a default, never inherited, and
  reported in the sidebar as an error rather than a caption.
- **Approvals.** A command matching a profile's `approve` prefixes interrupts
  the graph and waits for a person. The decision resumes the same turn on the
  same thread — which works only because 0.4.0 made checkpoints durable, so a
  pending approval survives a browser reload. Rejection reaches the model as a
  `ToolMessage`, keeping the conversation usable.
- **Per-run ceilings, todos and context editing**, all driven by the profile
  and all off unless it asks. `exit_behavior="end"` on the ceilings, so a run
  that is cut short still closes its tool calls.
- `docs/PROFILES.md`, and `example_profiles.json` as a starting point.

### Security

0.2.0 removed a shipped shell server because a shell alongside a web-search
tool is an indirect prompt-injection path to credential exfiltration. Making
the shell the point does not retire that reasoning, so the capability was
audited before release. What the audit found, all demonstrated by running it:

- **`>(` was missing from the substitution refusal** while `<(` was there, so
  an allowlist of nothing but `ls` still permitted `ls > >(sh -c id)` — no
  chaining operator and no separator, nothing for a word-splitter to see.
- **An option or a quote could hide a subcommand from an approval rule.**
  `git -c user.name=x push`, `git -C /tmp push` and `git "push"` all ran a push
  and all walked past a rule of `git push`. Rules now match in order rather
  than by adjacency, and over-match rather than under-match.
- **The first version of `example_profiles.json` was self-defeating**, listing
  `python`, `make`, `find` and `pytest`. Listing an interpreter is listing
  `sh`. A test now reads the allowlist entries of every shipped profile and
  refuses an interpreter among them, a second constrains the argument denylist
  (`-c`, `-exec`, `--pre`, `ext::`) against nine spawning commands, and a third
  catches an approval rule for a command the allowlist refuses — which can
  never fire. The first two were one test until a documentation review measured
  it: the probes are refused by the denylist whatever the allowlist says, so it
  never constrained the allowlist it was written to guard.
- **A profile chose what was bind-mounted into the sandbox.**
  `workspace_root: "/root"` produced `docker run -v /root:/root`, and
  `--read-only` does not cover bind mounts.
- **The sandbox has no network; the browser does.** An image embed in an
  assistant reply is fetched by the viewer with no click, so the container's
  missing network does nothing about it. Image embeds are now defused in both
  the streamed and the replayed path. The docstring that claimed the
  exfiltration step "has nowhere to go" was the sentence the feature's
  justification hung on, and it was wrong.
- Refused command lines are no longer logged verbatim, and shell output is
  redacted for provider-token shapes before the model sees it — otherwise it
  lands in `data/checkpoints.db`, unencrypted, until the conversation is
  deleted.

A second pre-release pass, this time over the tests rather than the code, found
22 of 47 mutations surviving — every *rule* pinned and almost every line that
*connects* a rule to the running agent free. Two mattered:

- **`execution_policy=` was unconstrained**, and `ShellToolMiddleware`'s own
  default when handed no policy is `HostExecutionPolicy`. That line going
  missing would not have disabled the sandbox; it would have moved every
  command onto the host. It is now both tested and checked at build time —
  a shell that did not take the policy it was given is refused rather than
  returned.
- **`git` and `rg` are interpreters given the right option**, and both are
  listed in the shipped examples because they are the point of a repository
  profile. `git -c core.pager='sh -c id'`, `git clone ext::sh` and
  `rg --pre /bin/sh` all ran. The docstring claiming the examples "name no
  interpreter, and a test enforces that" was true of the test and false of the
  file. Those argument forms are refused now, as an explicitly incomplete
  denylist, and the claim says what it actually covers.

**Unverified:** no Docker daemon was available while building this, so the
container isolation flags are set and read but were not observed running.
Before enabling the shell, run this **where the app runs**, with the flags the
policy actually passes:

```bash
docker run --rm --network none --read-only --user nobody \
  python:3.12-slim /bin/bash -c 'echo ok'
```

The default image was Alpine until the audit, which ships no `/bin/bash`. And
note where the app runs: **the image built by `dockers/Dockerfile` has no
Docker CLI and the compose file mounts no socket**, so the shell capability
cannot run under Compose at all. Giving the app container the host daemon would
make a sandbox escape a host compromise, which is the opposite of the point.

### Fixed

- The allowlist guard was defined as a sync `wrap_tool_call` while the app
  drives the graph with `astream`, so LangGraph would have raised
  `NotImplementedError` on every shell call and the guard would never have run.
  Its tests called the sync hook directly and passed — the method production
  never reaches. Never released.
- Two tool calls in one model message raise a single interrupt carrying both,
  and answering it with one decision raises, wedging the thread. Every stopped
  action is now shown and answered. Never released.
- Only the inline form of a markdown image was defused in assistant text, so
  `![a][ref]` with a link definition kept the whole exfiltration route open
  while the module docstring said it was shut. Both forms are now defused.
  Never released.
- `example_profiles.json` asked for `workspace_root: "/workspace"` while both
  `.env.example` files suggested `MCP_WORKSPACE_ROOT=/srv/workspaces`, so an
  operator following both got a flagship profile whose workspace was silently
  refused — no mount, `-w /`, read-only root, a log line and nothing else. The
  two now agree, and a test asserts they keep agreeing. Never released.
- The `repository` example approved four git subcommands while its allowlist
  permitted every one of them, so `git commit`, `git rebase`, `git merge`,
  `git rm`, `git branch -D` and `git stash drop` ran with nobody asked. All of
  them now stop, and the profile's description says so instead of claiming the
  profile only reads. Never released.
- `PROMPT_CACHE_TTL` fell back on a *typo* but not on an *empty* value, while
  `docker-compose.yaml` passes `PROMPT_CACHE_TTL=${PROMPT_CACHE_TTL:-}` — which
  sets it to the empty string. Every agent build under Compose logged a warning
  about a value nobody typed. Read-then-`or`, matching `shell.py` and
  `sessions.py`.

---

## [0.4.1] — 2026-09-09

Ten runtime defects, found in three passes: reviewing 0.4.0 against the plan
for the next version, mutating the fixes that came out of it, then checking the
documentation against what the code had become. The first had broken every turn
since 0.3.0. Four were introduced by the fixes above them and never released;
the other six shipped.

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
  fails it, so a key of the shape Smithery's own COPY button produces —
  `@smithery-ai/server-sequential-thinking` — would have failed every turn
  rather than one tool, and step 3 of `README.md` is an instruction to paste
  exactly that. `namespaced()` now cleans the prefix and shortens it to fit,
  keeping the tool's own name whole. Never released.
- **`Turn`'s deadline was dead code.** It was consulted only when collecting
  the result, which `app.py` reaches only after iteration has already ended, so
  the guarantee it documented — that a stuck loop cannot hold the browser —
  did not hold. Measured, not read: a stub that blocks the loop thread hung the
  turn indefinitely. Iteration now consults it, and the collect waits out only
  what is left of it. A second pass found the first fix incomplete: the check
  sat in the "event queue is empty" branch, so a stream that keeps producing
  was still unbounded — 3.05s of events against a 0.10s deadline. It is now
  checked before the wait as well. Never released.
- **`Turn` was free to stop forwarding the turn budget** to `run_query`, with
  every test green. That timeout exists so a turn cut short still reports its
  partial text and spent tokens; without it the only remaining bound reports
  "did not respond" and discards both. Never released.
- **A collision report named the wrong thing to fix.** Two configuration keys
  that shorten to the same tool prefix were reported as "rename them at the
  server", which is not where the problem is, and the failure's `server_name`
  was filled with the *tool's* name, so the sidebar labelled a tool as a
  server. It now tracks which servers produced each name and gives the remedy
  that matches — including for a server whose tool names are entirely outside
  `[a-zA-Z0-9_-]` and therefore all clean to the same string. Never released.
- **A turn that produced nothing was reported as success**, appending an empty
  assistant bubble. The changelog has claimed since 0.3.0 that this was fixed;
  it was not.
- **A tool was allowed to outlive the turn that called it, silently.** Both
  bounds are configurable and their defaults meet exactly at 60s — the turn
  slider's own minimum — so the state the documentation tells you to avoid was
  one drag away with nothing saying you had arrived. The sidebar now says so.
- **`__version__` said 0.3.0** while `pyproject.toml` and the Compose image tag
  said 0.4.1. Nothing imports it, so nothing broke and nothing noticed for two
  releases; it is now read from the installed distribution, and a test fails
  when the three disagree.

### Added

- `tests/test_turns.py` — the first tests in this repository that drive a real
  chat turn through the real script. Their absence is why a streaming path that
  raised on every write shipped and stayed shipped.

  A mutation pass over the first version of that file found 18 of 24 mutations
  surviving, all of them in the streaming path the release is named after: the
  turn ends in `st.rerun()`, so every assertion read a page rebuilt from the
  transcript rather than from the events. Deleting the streaming loop from
  `app.py` left the whole suite green. The file now also drives `Turn`
  directly.

  A second pass over *that* found 24 of 48 still surviving — the same hole one
  level up, since `app.py` was held by a single spy watching iteration and
  nothing else. Both rounds of survivors are now dead, checked one mutant at a
  time.
- Documentation corrections that were themselves defects. The turn diagram in
  `docs/ARCHITECTURE.md` still drew `run_sync(run_query(...))` — the exact call
  this release removed — and `CLAUDE.md` sends every agent to that file before
  they touch this wiring, so the one diagram they read taught the bug. The
  README screenshot was three versions stale: a retired model, slider ranges
  that no longer exist, and a Registered Tools List containing
  `desktop-commander` and `tavily-mcp` together — the shell plus web-search
  pair the same README says was removed in 0.2.0 as a credential-exfiltration
  path. Retaken against 0.4.1. `PROMPT_CACHE_TTL` and `MCP_CONFIG_PATH` were
  read by the code and documented in no `.env.example`, and the Docker one had
  not been given this release's or 0.4.0's variables at all.
- `tests/test_rendering.py`, and `mcp_agent/rendering.py` for it to test.
  `draw` moved out of `app.py` because nothing could reach it there: every one
  of its branches could be broken with the suite green, including sending
  untrusted tool output through `st.markdown`, which is the markdown escape the
  comment beside it exists to prevent.

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
