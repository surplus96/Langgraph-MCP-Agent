# Modernization Plan — Langgraph-MCP-Agent

**Date:** 2026-09-04
**Basis:** four independent agent reviews (code quality, security, pipeline, documentation)
**Current state:** non-functional as shipped; ~1,200 LOC across two modules; last substantive change 2025-07

---

## Why the app does not run today

Three independent blockers, each sufficient on its own:

1. **All five selectable model IDs are retired or superseded** (`app.py:187-192`). Every
   request fails at the provider.
2. **`temperature=0.1` (`app.py:467`) is rejected with HTTP 400** by every current Claude
   model — sampling params were removed, not deprecated. A naive model-ID swap still fails.
3. **`uv.lock:1131` pins `langchain-mcp-adapters==0.0.7`, whose API is incompatible with
   `app.py:452-453`.** In 0.0.7 `get_tools()` is synchronous, so `await` on it raises
   `TypeError`; the client is an async context manager that the code never enters, so no
   MCP subprocess is ever started. Verified by reading both 0.0.7 and 0.3.2 source.
   `pip install -r requirements.txt` happens to resolve to 0.3.2 and works — so this
   fails only for `uv` users, which is why it went unnoticed.

Separately, **neither documented install path works from a clean clone** (no `Dockerfile`
exists; `cp .env.example .env` targets a file that isn't there; both clone URLs 404).

---

## Phase 0 — Do now, independent of any code change

Not code work. Owner action required.

| # | Action |
|---|---|
| 0.1 | **Rotate the Smithery API key** (`config.json:10`, `:22`) — public since 2025-07-08, ~14 months. Rotate at the provider *first*; the blob stays reachable in every fork regardless of what happens to this copy. Revoke the Tavily profile (`config.json:24`). |
| 0.2 | **Rotate `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `LANGSMITH_API_KEY`** if `dockers/.env.example:1-3` was produced by truncating a live `.env` — the placeholders carry real-looking key prefixes. Audit provider usage logs back to 2025-07-08. |
| 0.3 | **Take any network-reachable deployment offline.** The documented `docker compose up -d` path yields an unauthenticated remote-shell endpoint. Treat such a host as potentially compromised; inspect its `config.json` for entries you did not add. |

---

## Phase 1 — Make it run again

Goal: a working app on current models. Smallest change set that clears the three blockers.

1. **Dependency floor bump.** `langchain-mcp-adapters>=0.3.2,<0.4`, `mcp[cli]>=1.24.0,<2.0.0`
   (0.3.2 requires `mcp<2`), `langgraph>=1.2.11`, `langchain>=1.4.0`,
   `langchain-anthropic>=1.7.1`, `langchain-openai>=1.6.0`, `streamlit>=1.63.0`.
   Regenerate `uv.lock`.
2. **Drop unused dependencies** in the same commit — `torch`, `transformers`, `selenium`,
   `beautifulsoup4`, `webdriver-manager`, `fastapi`, `pillow`, `faiss-cpu`, `pymupdf`,
   `jupyter`, `notebook`, `langchain-community`, `requests`. Zero import sites for any of
   them. Removes multi-GB from image builds and shrinks the lock regeneration.
3. **Single model registry.** Collapse the five duplicated model lists
   (`app.py:187-192`, `:205`, `:460-463`, `:498-509`, `:517`) into one
   `MODEL_REGISTRY` mapping id → `{provider, max_tokens, supports_temperature}`.
   Dispatch on `provider`, not on list membership.
4. **Model migration** (Anthropic IDs verified against the current catalog):

   | Retired | Replacement | Max output | Notes |
   |---|---|---|---|
   | `claude-3-7-sonnet-latest` | `claude-opus-5` | 128K | thinking on by default; no `temperature` |
   | `claude-3-5-sonnet-latest` | `claude-sonnet-5` | 128K | new tokenizer — re-baseline token budgets |
   | `claude-3-5-haiku-latest` | `claude-haiku-4-5` | 64K | still accepts `temperature`; old thinking config |
   | `gpt-4o` / `gpt-4o-mini` | dropped — Anthropic only | — | Re-add once IDs are confirmed |

5. **Remove `temperature`.** Raise `max_tokens` to the real ceilings (previously
   6–16× too low). VERIFIED 2026-09-04 against `GET /v1/models`: all three
   registry ids exist and every `max_tokens` matches the API exactly.
6. **Delete `cleanup_mcp_client`** (`app.py:216-231`). In 0.3.2 `__aexit__` raises
   `NotImplementedError` unconditionally; the bare `except` swallows it, so line 226
   (`= None`) is never reached. Replace with an explicit assignment.
7. **Fix `thread_id`** — `configurable={"thread_id": ...}`, not a top-level
   `RunnableConfig` key (`app.py:408-411`). Currently works only via an
   `ensure_config` fallback that the 0.3 → 1.x jump may tighten; if it breaks,
   conversation memory silently stops threading with no error.
8. **`create_react_agent` → `create_agent`** (`langchain.agents`); `prompt=` →
   `system_prompt=`; `MemorySaver` → `InMemorySaver`.

**Exit criterion:** app starts, connects MCP tools, completes a tool-using turn on each
registry model.

---

## Phase 2 — Security hardening

1. **Stop shipping secrets.** Add `config.json` to `.gitignore`; ship only
   `example_config.json`; resolve `${ENV_VAR}` references in config values instead of
   storing literals. Add a `.dockerignore` (`.git`, `.env*`, `config.json`, `data/`) —
   none exists, so any future build copies the leaked history into the image.
2. **Close the auth bypass.** Compose sets `USER_ID=${USER_ID:-}`, so an unset var becomes
   `""` and a blank login form satisfies `"" == ""` (`app.py:118-121`). Drop the `:-`
   defaults; refuse to start when `USE_LOGIN=true` and either credential is empty; reject
   empty submissions before comparison; compare with `hmac.compare_digest`.
3. **Flip insecure defaults.** `USE_LOGIN=true`; bind `127.0.0.1:8585:8585` rather than
   the wildcard; replace `admin`/`admin1234` in the example with `REPLACE_ME` placeholders;
   default `LANGSMITH_TRACING=false` (currently ships every prompt and tool result to a
   third party with no in-app disclosure).
4. **Constrain MCP registration.** The sidebar takes free-form JSON and spawns it as a
   subprocess (`app.py:634` → `app.py:452`) with no allowlist, persisted to disk and
   applied to every user. Gate behind an admin role or an env flag, and validate `command`
   against an allowlist.
5. **Break the prompt-injection chain.** The default `config.json` pairs
   `desktop-commander` (shell + filesystem) with `tavily-mcp` (untrusted web content) in
   one agent with no tool approval — an exfiltration path needing *no* attacker UI access.
   Ship a benign default config; drop the "tool output outranks your knowledge" line from
   the system prompt (`app.py:139-179`), which actively reinforces it.
6. **Render tool output with `st.code`, not `st.markdown`** (`app.py:369-373`) — a literal
   fence in tool output escapes into live markdown, and auto-fetched images are an
   exfiltration channel.
7. **Pin `npx` versions.** `npx -y ...@latest` fetches and executes unpinned remote code at
   every startup with the app's privileges.
8. **Add `pip-audit` to CI.** ~⅓ of the installed tree is absent from `uv.lock` entirely.

---

## Phase 3 — Structure and tooling

1. **Delete dead code first** (~250 lines, zero behavior change): `ainvoke_graph`
   (`utils.py:214-322`, no callers), the unreachable `updates` branch and CLI-print
   fallbacks in `astream_graph`, the dead nested `format_namespace` (`utils.py:40-41`),
   `mcp_config_text`, the `mcp_tools_expander` flag that can never become `True`.
   `utils.py` drops from 322 to ~50 lines.
2. **Add the gate:** ruff (`select = ["E","F","I","B","UP","ASYNC"]`, `target-version =
   "py312"`), ruff-format, mypy, a dev dependency group, `.pre-commit-config.yaml`, one CI
   job. Ruff's `B006`/`F841` alone catch three findings on day one.
3. **Real error handling.** `initialize_session` has no `try`, so its caller's error branch
   (`app.py:790`) is unreachable dead code and any MCP misconfiguration replaces the app
   with a traceback. Add logging; never leave an `except` body that is only a dead import.
4. **Extract pure modules** — `src/mcp_agent/{config,models,agent}.py`, Streamlit-free and
   therefore testable; `app.py` becomes an import-and-render shell. Add packaging config.
5. **First tests:** config round-trip and corrupt-file behavior, registry lookup for every
   entry, streaming callback against recorded `AIMessageChunk` fixtures.
6. **Remove the async scaffolding.** Delete `nest_asyncio`, the Windows policy no-op (a
   no-op since Python 3.8), and the per-session loop that is never closed. Replace with one
   daemon-thread loop via `run_coroutine_threadsafe`. Do this *after* tests exist —
   `nest_asyncio` is a known source of anyio cancel-scope corruption in this exact stack.
7. **Type the surface.** `from __future__ import annotations`, PEP 585/604 throughout, a
   `MCPServerConfig` TypedDict replacing the ad-hoc validation ladder.
8. **Consolidate state.** One `AppState` dataclass replacing 15 loose `session_state` keys
   initialized across six sites under four different guard idioms.

---

## Phase 4 — Pipeline optimization

**Status: items 1-2 landed 2026-09-04.** Caching middleware and token accounting
are in; the hit rate is surfaced in the sidebar. Items 3-9 remain.

Ordered by value. Requires Phase 1; step 4.2 should follow Phase 3.6.

1. **Prompt caching — the largest cost lever.** The system prompt *and* the full MCP tool
   schema block are re-sent at full input price on every model call — not once per user
   turn, but once per ReAct iteration, up to 50 at the default `recursion_limit`. Both are
   perfectly stable prefixes. Add `AnthropicPromptCachingMiddleware`; **sort tools by name
   first** or the cache silently misses. Anthropic cache reads are ~0.1× input.
2. **Token and cost accounting.** Nothing currently reads `usage_metadata`. Without it you
   cannot verify step 4.1 worked — assert `cache_read_input_tokens > 0` on turn 2.
3. **Effort control.** Every request currently runs at default effort regardless of whether
   it is "what time is it" or multi-step research. Expose a sidebar selector; use
   `thinking={"type":"adaptive","display":"summarized"}` — the default `"omitted"` renders
   as a long dead pause in a streaming UI.
4. **Retry, timeout, partial output.** No `max_retries`, no per-request timeout; one coarse
   whole-turn `wait_for` that **discards every token already streamed and paid for** on
   expiry. Cap the 30,000-second slider (8.3 hours) at 600s.
5. **Multi-block streaming.** `app.py:288-297` inspects only `content[0]`; parallel tool
   calls and interleaved thinking produce multi-block chunks whose remainder is dropped.
   Blocking for step 4.3.
6. **Cache tool schemas** (`@st.cache_resource` keyed on config hash) — currently a full
   connect/list/disconnect per Apply Settings, seconds per `npx` server.
7. **Session reuse.** 0.3.x creates a new MCP session *per tool call* — with stdio that is
   a process fork per invocation, up to ~50 per turn. Measure before assuming it is fine.
8. **Durable checkpointer.** `InMemorySaver` is constructed inside `initialize_session`, so
   every Apply Settings silently discards conversation state while the UI still shows the
   old history. Hoist behind `@st.cache_resource`; consider `langgraph-checkpoint-sqlite`.
9. **History trimming.** History is stored twice and grows unbounded; every turn re-sends
   all tool-result blobs at full price.

---

## Phase 5 — Documentation

1. **Fix what is actively wrong:** both clone URLs (`MCP-Agnet.git` typo / wrong `cd`
   target), `cp .env.example .env` (file is at `dockers/.env.example`), the `.env` location
   prose, `claude-3-haiku-latest` (does not exist — it is `claude-3-5-haiku-latest`), the
   `blob`-URL screenshot that cannot render, and the 8501-vs-8585 port confusion.
2. **Document what is missing:** Node.js/npm as a hard prerequisite (the shipped config
   launches everything via `npx`); `USE_LOGIN`/`USER_ID`/`USER_PASSWORD`, read by code and
   documented nowhere; `config.json` itself and its lifecycle; every sidebar control.
   Mark the four LangSmith vars as SDK-consumed — no application code reads them.
3. **Add `LICENSE`.** MIT is claimed in three places with no file; as distributed the repo
   is unlicensed. Retain upstream attribution to `teddylee777/langgraph-mcp-agents`.
4. **Resolve the Docker path** (decision D3) — write `dockers/Dockerfile`, or remove the
   `build:` stanzas and the section.
5. **Normalize code language.** `utils.py` docstrings are Korean, `app.py` is English;
   pick English for code, keep Korean at README level where it is intentional.
6. **Port the Korean tool-add walkthrough** (`README_KOR.md:143-152`, verified accurate
   against the code) into `README.md`, which omits it entirely. Fix the missing Docker
   Desktop link in the Korean README. Remove the hands-on-tutorial section promising a
   notebook that does not exist, or ship the notebook.
7. **Add** `docs/ARCHITECTURE.md`, `docs/MCP_TOOLS.md`, `CONTRIBUTING.md`, `SECURITY.md`,
   `CHANGELOG.md`, `CLAUDE.md`.

---

## Open questions

- **OpenAI model IDs are UNVERIFIED.** `platform.openai.com` is blocked by this
  environment's egress proxy. `gpt-5.6-sol`/`-terra`/`-luna` are corroborated by an AWS
  Bedrock announcement and several trackers, but not by first-party docs. Whether bare
  `gpt-4o` still resolves on the API is also unconfirmed.
- ~~**`claude-haiku-4-5` vs `claude-haiku-4-5-20251001`**~~ — RESOLVED 2026-09-04.
  Only the dated form exists; the bare alias is not in the Models API listing.
- **Git history purge** for the leaked key requires a force-push and does not help existing
  forks. Rotation (0.1) is what actually mitigates.
