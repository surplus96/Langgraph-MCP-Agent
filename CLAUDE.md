# CLAUDE.md

Guidance for Claude Code working in this repository. Human contributors want
[CONTRIBUTING.md](CONTRIBUTING.md); this file is the subset that AI agents get
wrong.

## Orientation

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before touching
`src/mcp_agent/sessions.py`, `runtime.py`, `turns.py`, or the middleware wiring
in `agent.py`. All four are load-bearing in ways that are not visible from the
code alone, and each carries a comment recording the bug that shaped it. Those
comments are the design record. Do not delete one because it "explains what the
code already says" — it does not, it explains what the code is not doing.

## Commands

```bash
uv sync                                   # install, dev deps included
uv run streamlit run app.py --server.port 8585
uv run ruff check . && uv run ruff format --check .
uv run mypy src/mcp_agent app.py
uv run pytest -q                          # 443 tests, no network, no API key
uv run python scripts/check_models.py     # verify the registry against the live API
```

Run all four checks before claiming a change is done. CI additionally runs
`pip-audit`, `gitleaks` over the full history, and a Docker build.

## Rules specific to this repository

**`app.py` is presentation only.** If you are about to add logic there, it
belongs in `src/mcp_agent/` instead. The test for this is whether you would want
to unit test it.

**`usage.py` must stay a leaf.** `state.py` imports it, and that import must not
pull in LangChain or an event loop.

**Anything touching an MCP session must run on the loop in `runtime.py`.** An
anyio cancel scope must be exited by the task that entered it. `SessionPool` is
not thread-safe, on purpose.

**Never send `temperature` to a reasoning model** — it conflicts with the effort
and thinking settings. `build_model` already handles this; do not reintroduce it.

**Model IDs are verified, not remembered.** Every entry in `MODEL_REGISTRY`
carries a dated verification comment. If you change one, run
`scripts/check_models.py` and update the date. Do not add a model ID because it
looks plausible; several plausible ones do not exist.

**English in code and docstrings.** Korean belongs in `README_KOR.md`, and both
READMEs must stay in step.

## Editing

Assert that an edit matched. A silent `str.replace` no-op has landed a
half-applied fix in this repository more than once — a timeout fix reached
`agent.py` and never reached `app.py`, while the commit message said it was
fixed. If a splice is getting complicated, rewrite the file rather than patching
it blind.

## The bar for "tested"

**A green suite is not evidence. A test counts only if breaking the code breaks
the test.**

Before saying something is tested, delete or invert the line you believe is
under test and confirm the suite goes red. Then revert.

Measured results from this repository, all of which were surprises:

- Deleting the prompt-caching middleware left all 77 tests green. The tests were
  characterising the library, which passes whether or not this project uses it.
- A mutation pass later found 3 of 7 mutations surviving.
- A cache-write bug reported as live turned out to be latent, because the
  streaming path carries no TTL breakdown. Severity is what happens, not what
  could happen.

The `test-engineer` agent exists to run this check. Use it rather than
self-certifying.

## The bar for a number

Every performance figure in this repository came from measurement:
610 ms → 10.5 ms locally, 770 ms → 2.4 ms on `npx`. One of them was wrong on the
first pass — the benchmark was timing a validation-error path rather than a real
tool call, which made the gain look like 609x instead of 319x.

If you state a number, say how you got it. If you did not measure it, say that
instead.

## Reporting

Say what you ran and what you only read. "The tests pass" and "the code looks
correct" are different claims, and conflating them is the failure mode that
costs the most here. If you skipped a verification step, say which one — do not
report the work as verified.

**End every summary with who did what.** One short line per piece of work,
naming the agent that took it — or saying plainly that none was needed and why.
Both halves matter: a list that only ever names agents is not evidence of
judgement, it is evidence of ceremony. Keep it to a line each; the reasoning
belongs in the body of the report.

```
- Secret-scanning CI     → security-reviewer. Found three bypasses I had missed.
- Documentation set      → docs-reviewer. Found two live defects.
- v0.3.0 tag             → no agent. One command, and it either works or 403s.
```

## Secrets

`config.json`, `.env` and `data/` are gitignored, and gitleaks runs pre-commit
and in CI. If a scan flags your change, fix the change. `.gitleaksignore` is for
historical, already-rotated findings only, and each entry must say so.

Do not try to make the scan pass by configuring it. Every route was reproduced
and closed: CI refuses to run if a `.gitleaks.toml` exists, `--ignore-gitleaks-allow`
neutralises a `# gitleaks:allow` comment, and a pull request touching
`.gitleaksignore` fails. A red `secrets` job means there is a secret, not that
the scanner needs tuning.

Never weaken the `${VAR:?message}` guards in `dockers/docker-compose.yaml` to
satisfy a scanner. They contain no secret; the flag is a false positive and the
guard is what makes the deployment fail closed.

The history contains credentials that were all revoked on 2025-09-04: an OpenAI
key and a LangSmith key in `dockers/.env.example`, and a Smithery key and
profile in `config.json`, at commits `5cd21de` and `c75d5c5`. The history is not
being purged — that decision is made, with reasons in
[SECURITY.md](SECURITY.md). Do not propose rewriting it.

## The agent team

`.claude/agents/` defines nine specialists — five auditors, three
diagnosticians, one verifier. [Its README](.claude/agents/README.md) routes by
question type, and lists what was deliberately *not* created and what would
justify adding it.

Use them. Reviewing your own work in the same context that produced it is how
the caching no-op, the surviving mutations, and the half-landed timeout fix all
got through the first time. Spawn the specialist whose question you actually
have, rather than a general reviewer.

Then record it: every summary ends with the who-did-what list described under
[Reporting](#reporting). "No agent needed, here is why" is a valid and expected
entry — a rename, a version bump or a one-command fix does not earn a review,
and spawning one to look busy wastes the reviewer's value along with the time.
