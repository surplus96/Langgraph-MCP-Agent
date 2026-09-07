# Review and diagnosis agents

Nine specialists. Pick by **what kind of question you have**, not by which files
are involved.

## Auditors — "is there a problem I have not noticed?"

Scan for potential problems. Read-only. Run in parallel for a broad review.

| Agent | Answers |
|---|---|
| `code-quality-reviewer` | Is this maintainable? Structure, typing, error handling, state, dead code, tooling gaps. |
| `security-reviewer` | Can this be attacked? Secrets, authn, injection, MCP trust boundary, containers, CVEs. |
| `pipeline-optimizer` | Is the agent runtime current and token-efficient? Model IDs, LangGraph/MCP wiring, async, dependencies, caching. |
| `docs-reviewer` | Do the docs match the code? Setup accuracy, drift, EN/KOR parity, missing documents. |
| `interface-reviewer` | Does the running app tell the user the truth? In-app copy, error messages, numbers, empty states. |

## Diagnosticians — "something is broken, why?"

Start from an observed failure and work back to a cause.

| Agent | Answers |
|---|---|
| `debugger` | Why does this test fail / this exception fire / this output look wrong? |
| `build-doctor` | Why does this not build, install, resolve, or run in CI? |
| `performance-profiler` | Where does the time go? What accumulates? Why is this slow? |

## Verifier — "do I actually know that?"

| Agent | Answers |
|---|---|
| `test-engineer` | Would these tests fail if the code were wrong? Breaks the code on purpose and checks whether the suite notices. |

## Routing

- Nothing failing, you want to know what is weak → **auditor**.
- Something failing → **diagnostician**. A failing assertion is `debugger`; a
  failing pipeline, image or lockfile is `build-doctor`; slow-but-correct is
  `performance-profiler`. If the code never got to run, it is `build-doctor`.
- A green suite you are about to trust → **`test-engineer`**.
- Words the user reads: in the app → `interface-reviewer`; in the README →
  `docs-reviewer`.
- Cost in tokens → `pipeline-optimizer`. Cost in seconds → `performance-profiler`.

## When to run them

Not optional, and not only when someone asks:

- **Before trusting your own verification of a non-trivial change.** This is the
  rule that was learned the hard way — see below.
- Before merging anything that touches security, dependencies, or user-facing
  behaviour.
- After a review's fixes, if the fixes were substantial.

## Why these exist, in the order they were learned

**The four auditors came first** and immediately earned their place: a live API
key in a public repo, an authentication bypass where an unset variable became an
empty string that matched an empty submission, a lockfile pinning a version whose
API the code could not call, and two documented install paths that both failed
from a clean clone.

**The two diagnosticians came next**, because three real failures during that
work — a CI job whose trigger matched no event so it never ran, a Docker build
missing a file that packaging metadata made a build input, and an audit tool that
aborted before checking anything while merely looking red — were all handled ad
hoc. No auditor covers *reproduce, isolate, root-cause*.

**The last three came from reviewing a change that had already been declared
verified.** The review found that prompt caching was inactive on two of three
models because the prefix fell under Anthropic's minimum cacheable length, which
is ignored silently; that the sidebar blamed a 0% cache hit rate on the one cause
it could not have been, sending a reader to debug a problem they did not have;
and that deleting the caching middleware entirely left the whole suite green.

Those three failures map exactly onto the three roles that did not exist:
`pipeline-optimizer` found the first because it was in its lane, but nobody owned
the second, and nobody owned the third. Hence `interface-reviewer`,
`test-engineer`, and — for the resource costs that kept being flagged and never
measured — `performance-profiler`.

## Deliberately not created

Roles worth adding only when the repository gives evidence for them, rather than
because a generic team template lists them:

- **data-modeler** — this project has no database. Add one when persistent
  schemas, migrations or query patterns appear.
- **api-designer** — nothing here exposes an API to other programs. Add one when
  something publishes a contract others depend on.
- **release-manager** — versioning, changelogs and migration notes are currently
  small enough to sit with `docs-reviewer`. Add one when releases have
  consumers who can be broken.
- **planner / architect** — the orchestrating session already holds the context a
  planner would have to rebuild from cold. Spawning one duplicates work rather
  than dividing it.

An agent with no evidence behind it dilutes routing and costs tokens on every
review. Add on demonstrated need.
