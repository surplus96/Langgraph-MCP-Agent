# Review and diagnosis agents

Six specialists. Pick by **what kind of question you have**, not by which files
are involved.

## Auditors — "is there a problem I have not noticed?"

Scan the codebase and report potential problems. Read-only. Run them in parallel
when doing a broad review; they are written to stay in their own lanes and defer
to each other on overlaps.

| Agent | Answers |
|---|---|
| `code-quality-reviewer` | Is this maintainable? Structure, typing, error handling, state, dead code, tooling gaps. |
| `security-reviewer` | Can this be attacked? Secrets, authn, injection, MCP trust boundary, container hardening, CVEs. |
| `pipeline-optimizer` | Is the agent runtime current and efficient? Model IDs, LangGraph/MCP wiring, async, dependencies, token cost. |
| `docs-reviewer` | Do the docs match the code? Setup accuracy, drift, EN/KOR parity, missing documents. |

## Diagnosticians — "something is broken, why?"

Start from an observed failure and work back to a cause. Use these instead of
guessing at a stack trace or a red pipeline.

| Agent | Answers |
|---|---|
| `debugger` | Why does this test fail / this exception fire / this output look wrong / this hang? |
| `build-doctor` | Why does this not build, install, resolve, or run in CI? |

## Routing

- Nothing is failing, you want to know what is weak → **auditor**.
- Something is failing → **diagnostician**.
- A failing *assertion or behaviour* → `debugger`.
- A failing *pipeline, image, install or lockfile* → `build-doctor`.
- Unsure between the two: if the code never got to run, it is `build-doctor`.

Auditors find problems that have not bitten yet. Diagnosticians explain problems
that already have. Sending a red CI log to an auditor gets you a list of unrelated
code smells; sending a refactor question to a debugger gets you "nothing is
failing".

## Why the diagnosticians exist

The four auditors were used first and worked well, but three real failures during
that work — a CI job whose trigger matched no event, a Docker build missing a
declared readme as a build input, and an audit tool that aborted before checking
anything — were all handled ad hoc. None of the auditors covers *reproduce,
isolate, root-cause*. These two do.
