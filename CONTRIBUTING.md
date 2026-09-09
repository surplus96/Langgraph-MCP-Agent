# Contributing

Thanks for looking. This is a small project, so the process is short — but the
bar for what a change has to demonstrate is deliberately high in a couple of
specific places. Those are at the bottom, and they are the part worth reading.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node.js (MCP
servers are commonly launched with `npx`).

```bash
git clone https://github.com/surplus96/Langgraph-MCP-Agent.git
cd Langgraph-MCP-Agent
uv sync                              # installs dev dependencies too
uv run pre-commit install            # ruff, gitleaks, whitespace hooks
cp .env.example .env                 # then fill in ANTHROPIC_API_KEY
                                     # (dockers/.env is for Compose only:
                                     #  load_dotenv searches upward from
                                     #  app.py and never looks in dockers/)
```

Run it:

```bash
uv run streamlit run app.py --server.port 8585
```

## The checks

Everything CI runs, you can run:

```bash
uv run ruff check .                       # lint
uv run ruff format --check .              # format
uv run mypy src/mcp_agent app.py          # types
uv run pytest -q                          # tests
```

CI additionally runs `pip-audit` over the exported lockfile, `gitleaks` over
the full history, and a Docker build. None of them need an API key; no test in
this repository makes a network call.

## Where code goes

```
app.py              Streamlit only. Widgets, layout, copy.
src/mcp_agent/      Everything else.
tests/              Mirrors the package, one file per module.
```

**The rule: `app.py` must contain nothing you would want to unit test.** If a
change adds logic to `app.py`, that is a signal it belongs in the package
instead. What legitimately lives there — the login gate, the editor gate, the
caching-floor warning, the failure banner — is behaviour that only exists during
a script run, and `tests/test_app_smoke.py` drives it through `AppTest`.

`src/mcp_agent/usage.py` is a leaf module and must stay one — `state.py`
imports it, and that import must not pull in `langchain`, `langgraph`, Streamlit
or an event loop. `langchain_core` types are fine; it already imports one.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing anything in
`sessions.py`, `runtime.py`, `turns.py`, or the middleware wiring in `agent.py`.
Those four are load-bearing in non-obvious ways, and the comments explaining why
are in the code. `turns.py` is the one whose obvious implementation is wrong for
a reason nothing local tells you.

0.5.0 added three more, and [docs/PROFILES.md](docs/PROFILES.md) is their
reference:

- `profiles.py` — profiles as data, plus the allowlist and approval matching.
  It parses command lines, so every change to it is a security change; the
  comments say which review demonstrated which bypass.
- `shell.py` — the shell capability. Two rules hold here and neither is
  negotiable: it takes an operator switch **and** a profile field to enable, and
  a profile may not reach `HostExecutionPolicy` or mount anything outside
  `MCP_WORKSPACE_ROOT`. Never list an interpreter in a shipped example
  allowlist; `test_no_shipped_allowlist_names_an_interpreter` is the test that
  says so, and the nine-probe test beside it does not (it constrains the
  argument denylist and stays green with `python` listed).
- `approvals.py` — the human-in-the-loop gate. Its predicate re-checks the
  allowlist, and that is not redundant: the gate hooks `after_model` while the
  guard is a `wrap_tool_call`, so middleware list order cannot make the guard
  run first.

## Style

- Ruff, line length 100, rule set `E,F,I,B,UP,ASYNC,SIM`. The hook fixes most of
  it for you.
- Full type annotations on anything public. `from __future__ import annotations`
  at the top of every module.
- **Code and docstrings in English.** Korean belongs in `README_KOR.md`.
- Comments explain **why**, not what. A comment that restates the line above it
  is noise; a comment recording a decision, a measurement, or a bug that a
  previous version had is the most valuable thing in the file. Most of the
  comments in this codebase are of the second kind — please match that.

## Dependencies

```bash
uv add some-package          # runtime
uv add --dev some-package    # development
```

Commit the resulting `uv.lock`. Note that plain `uv lock` does a **minimal**
update — it leaves the transitive tree stale, which is how this project once
carried six known-vulnerable packages while the lockfile looked fresh. Use `uv
lock --upgrade` when you mean to refresh the whole resolution, and expect
`pip-audit` in CI to be the thing that catches you if you don't.

## Tests: the actual bar

A green suite is not evidence. **A test only counts if breaking the code it
covers breaks the test.** Before claiming a feature is tested, delete or invert
the line you think is under test and confirm the suite goes red.

This is not a rhetorical standard. Real results from this repository:

- Deleting the prompt-caching middleware from `build_agent` left all 77 tests
  green. The tests characterised the *library's* behaviour, which passes whether
  or not this project uses it. `test_build_agent_attaches_the_caching_middleware`
  exists because of that.
- A later mutation pass found 3 of 7 mutations surviving, including the per-TTL
  cache-write fallback that had just been added.
- A timeout fix landed in `agent.py` but silently failed to apply to `app.py`.
  The suite did not notice. It does now.

So, for a change to `src/mcp_agent/`:

1. Add tests in the matching `tests/test_*.py`.
2. Mutate the code you added — change a constant, drop a term from a sum, remove
   the call — and confirm the suite fails.
3. Revert the mutation.

If you cannot make the suite fail, the test is describing the library, not your
change.

Two more things worth knowing about the existing tests:

- `test_caching.py` intercepts the middleware's own public hook. The middleware's
  effect is invisible from the agent's return value — it changes the outbound
  request — so this is the only way "caching is wired up" is a checked claim.
  Whether the cache is *hit* still needs a live call.
- `test_app_smoke.py` uses Streamlit's `AppTest`, which resolves relative paths
  against the *test file*, not the working directory. That is why `APP` is built
  from `Path(__file__).parent.parent`.

## Verifying claims about models and APIs

Model IDs, context windows, token minimums and capability flags in
`src/mcp_agent/models.py` are checked against the live API, not against memory:

```bash
uv run python scripts/check_models.py
```

The registry carries a dated comment saying when it was last verified. If you
change it, re-run the script and update the date. A model ID that "looks right"
is not a model ID.

The script checks the ids and `max_tokens` against the Models API.
`min_cacheable_tokens` is **not** in that response — it comes from Anthropic's
prompt-caching documentation and has to be re-read by hand.

The same applies to performance claims. Numbers in this repository
(58x, 319x, 610 ms) came from measurement, and one of them was corrected
mid-flight when the first benchmark turned out to be timing a
validation-error path rather than a real tool call. If you add a number, say
how you got it.

## Secrets

`config.json`, `profiles.json`, `.env` and `data/` are gitignored, and gitleaks runs both
pre-commit and in CI. If gitleaks flags your commit, fix the commit — do not add
a fingerprint to `.gitleaksignore`. That file is only for historical findings
that have already been rotated, and each entry has to say so; CI fails a pull
request that touches it.

Three ways to switch the scan off were reproduced and then closed, so do not
expect them to work: CI refuses to run at all if a `.gitleaks.toml` exists in
the repository, it passes `--ignore-gitleaks-allow` so a `# gitleaks:allow`
comment has no effect there, and it fails if `config.json` is ever tracked. The
`# gitleaks:allow` hatch does still work in the pre-commit hook, where you are
looking at the result — use it there if you must, and expect CI to disagree.

Never weaken the `${VAR:?message}` guards in `dockers/docker-compose.yaml` to
quiet a scanner. See [SECURITY.md](SECURITY.md).

## Pull requests

- One concern per PR.
- Say what you changed and **why**, and what you did to convince yourself it
  works. "Tests pass" is not that; "I deleted the middleware and the suite went
  red" is.
- Update the docs in the same PR. `docs/ARCHITECTURE.md` describes design
  decisions, so a change that invalidates one of them should change the doc too.
- Add a `CHANGELOG.md` entry under `Unreleased` for anything user-visible.

## Commits

Imperative mood, present tense, explaining the change rather than restating the
diff:

```
Hold MCP sessions open instead of one per tool call
Summarize history before it hits the context limit
```

Not `fix bug` or `update sessions.py`.
