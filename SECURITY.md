# Security

## Reporting a vulnerability

Open a [private security advisory][advisory] on this repository. Please do not
open a public issue for anything exploitable.

[advisory]: https://github.com/surplus96/Langgraph-MCP-Agent/security/advisories/new

There is no formal response SLA — this is a small project maintained in
people's spare time. You will get an acknowledgement, and a fix or a clear
"won't fix" with reasons.

## What this application is

A Streamlit UI that lets a user register **MCP servers** and then lets an LLM
call the tools those servers expose. Registering a server **launches a
subprocess**. That single fact drives most of what follows: the trust boundary
is not "who can log in", it is "who can add a tool", and an LLM sits between
the two deciding what to invoke.

Treat this as a **single-user or trusted-team tool**, not a multi-tenant
service.

## Threat model

### In scope

| Risk | Control |
|---|---|
| Arbitrary command execution via a registered MCP server | `MCP_ALLOWED_COMMANDS` allowlist; `MCP_ALLOW_TOOL_EDIT` off by default |
| Arbitrary command execution via the **shell capability** | Off unless an operator *and* a profile both enable it; sandboxed with no network; per-profile allowlist; human approval for named prefixes. See [docs/PROFILES.md](docs/PROFILES.md) |
| A handed-over profile escalating privilege | `MCP_ENABLE_SHELL`, `MCP_SHELL_POLICY` and `MCP_WORKSPACE_ROOT` are operator-side; a profile cannot enable a shell, reach the host policy, or mount anything outside the operator's root |
| Unauthenticated access to the UI | Login gate that fails closed |
| Credentials committed to the repository | `config.json` and `.env` gitignored; gitleaks in CI and in pre-commit |
| Known-vulnerable dependencies | `pip-audit` in CI against the locked resolution |
| Container escape / privilege escalation | Non-root user, read-only rootfs, all capabilities dropped, `no-new-privileges` |

### Out of scope

- **Indirect prompt injection.** Tool output is attacker-controllable, and a
  model that reads a malicious web page can be steered. The system prompt tells
  the model to treat tool output as evidence rather than instructions. That is
  a mitigation, not a control — do not rely on it. The real control is not
  registering a data-fetching tool and a high-privilege tool in the same
  session.

  0.5.0 narrows what a steered model can *do* — the sandbox has no network, the
  allowlist bounds which commands exist, and consequential ones stop for a
  person — but none of that stops it being steered. **A profile that pairs a
  shell with a web-fetching MCP server rebuilds the exact combination 0.2.0
  removed**, and nothing in the code prevents you writing one. The shipped
  examples do not, and a test enforces that; your own profiles are yours.

- **Exfiltration through the page itself.** Assistant replies render as
  markdown, and an image embed is fetched by the viewer's browser without a
  click. The sandbox's missing network does nothing about that, because the
  request leaves the browser rather than the container.
  `mcp_agent.rendering.without_images` defuses image syntax in both the
  streamed and the replayed path, which closes the automatic case. Links are
  left intact — they need a click, and stripping them would cost the model its
  ability to cite anything — so a user who clicks a link the model wrote is
  still outside what this stops.
- **Multi-tenancy.** There is one credential pair for the whole deployment.
  Everyone who logs in shares the same MCP configuration and the same
  subprocess privileges.
- **Exposing the app to the internet.** See below.

## The controls, and what they actually do

### The login gate — fails closed

`USE_LOGIN=true` requires `USER_ID` and `USER_PASSWORD` to be non-empty. If
either is blank, the app **refuses to render a login form at all** rather than
accepting a submission.

This is not theoretical hardening. Docker Compose turns an unset variable into
an empty string, so the previous code compared `"" == ""` and authenticated
anyone who clicked the button. `dockers/docker-compose.yaml` now uses
`${USER_ID:?...}` / `${USER_PASSWORD:?...}` so Compose refuses to start rather
than starting insecurely.

> Secret scanners sometimes flag those `${VAR:?message}` lines as hardcoded
> credentials. They are shell parameter-expansion guards containing no secret.
> Mark them as false positives; **do not weaken the guard to silence a
> scanner.**

Credentials are compared with `hmac.compare_digest`, and blank submissions are
rejected before comparison.

This gate is a speed bump in front of a local tool. It is a single shared
credential pair over whatever transport you put in front of it, with no
lockout, no rate limiting, and no session expiry.

### The MCP command allowlist

`MCP_ALLOWED_COMMANDS` constrains what an MCP server entry's `command` field may
be. Default: `npx,uvx,node,python,python3,docker`.

Note what this does *not* do. `npx` can fetch and execute an arbitrary package
from the registry; `python` can run an arbitrary script. The allowlist stops
`command: "bash"`, not a malicious `args`. It narrows the shape of the attack,
it does not close it. **The only real control over what runs is who is allowed
to edit the configuration.**

### `MCP_ALLOW_TOOL_EDIT`

Off by default. With it off, the in-app tool editor is not rendered, and the
configuration can only be changed by editing `config.json` on disk (or on the
mounted volume). Turn it on only if you trust everyone who can reach the UI as
much as you trust yourself with a shell.

### Empty default configuration

`DEFAULT_CONFIG` ships empty. The default it replaced registered a shell tool
(`desktop-commander`) alongside a web-search tool. That combination is a
complete indirect-prompt-injection chain: fetch attacker text, follow its
instructions, run commands, exfiltrate credentials. It also referenced two
server scripts that do not exist in this repository.

### Container hardening

The image runs as UID 1000 (`useradd --uid 1000 appuser` in `dockers/Dockerfile`
— compose has no `user:` key, so an audit of compose alone will not find it).
`dockers/docker-compose.yaml` adds `read_only: true`,
`cap_drop: ALL`, `no-new-privileges:true`, a `pids_limit` and a `mem_limit`, and
binds the port to `127.0.0.1` only.

That last one matters most. **This app launches subprocesses on request. Do not
publish its port.** If you need remote access, put a reverse proxy with TLS and
real authentication in front of it.

## Secrets

### Where they belong

| Secret | Location | Committed? |
|---|---|---|
| `ANTHROPIC_API_KEY` | `dockers/.env` or the process environment | Never — `.env` is gitignored |
| MCP server credentials | `config.json` / the `data/` volume | Never — both gitignored |
| Login credentials | `dockers/.env` | Never |

`example_config.json` and `.env.example` are the committed templates. They
contain placeholders only.

### Scanning

- **`gitleaks`** runs in CI (`secrets` job) over the **full commit history** on
  every push and pull request, and in `.pre-commit-config.yaml` for local
  commits. Both use the same pinned version, so the two rulesets agree.
- **`.gitleaksignore`** holds fingerprints gitleaks should stop reporting. Only
  historical, already-rotated findings belong there. A finding in current code
  is a bug to fix, not a line to add.
- **`pip-audit`** runs in CI against the exported lockfile.

#### The scan is not configurable from inside the branch it scans

A secret scan that the scanned code can switch off is decoration. Three ways
that was possible were reproduced against this repository and then closed:

| Bypass | Reproduced | Closed by |
|---|---|---|
| A `.gitleaks.toml` in the repository, whose allowlist is `.*` | A committed live-shaped Anthropic key went from exit 1 to exit 0 | A guard step that fails the build if the file exists at all |
| A trailing `# gitleaks:allow` comment in the same commit as the secret | Same key, exit 0 | `--ignore-gitleaks-allow` in CI (the hatch stays in the pre-commit hook, where a human sees the result) |
| Appending the finding's own fingerprint to `.gitleaksignore` | Suppresses it in the same pull request | A guard step that fails a pull request touching that file |

CI also refuses a **tracked `config.json`**. That file is gitignored, and it
defeats the default rules by shape rather than by value: a key as a bare JSON
array element, with `--key` on the preceding line, gives gitleaks no adjacent
assignment to anchor on. A rule change would not reliably catch it; keeping the
file untracked does.

#### What a green scan does and does not mean

It means no *known-shape* secret was found. Measured against gitleaks 8.28.0's
default rules, `sk-ant-api03-…` and `sk-proj-…` keys are detected; a legacy
`sk-` + 48-character key, a bare `AKIA…` access key ID, and a UUID-shaped key
in a JSON array are not. `--max-decode-depth 2` is set so a base64-wrapped
secret is looked at, which the default depth of 0 does not do.

Read the scan as a floor, not a guarantee.

### Known historical exposure

Three API keys — Anthropic, OpenAI and LangSmith — were committed to
`dockers/.env.example` on 2025-07-08, before this modernization, and remained in
the history until found on 2025-09-04. In this repository's copy all three are
truncated fragments (15, 21 and 15 characters, each ending in `...`) rather than
complete keys, and only the OpenAI one carries enough entropy for the scanner to
flag it. They were treated as live regardless.

A Smithery API key and profile ID were also committed, in `config.json`, in the
same first commit and again at `c75d5c5`. Note that the secret scanner does
**not** find this one — it is a bare JSON array element with no adjacent
assignment keyword — which is why CI now refuses a tracked `config.json`
outright rather than relying on detection.

**All of these credentials were revoked on 2025-09-04**, and the Smithery
profiles and API keys were reset at the same time. No usage was recorded
against any of them.

The history is **deliberately not rewritten**. This is a public repository with
existing clones and forks, so a purge would not recall the secrets and would
break every existing checkout. Revocation is what made them harmless. The one
fingerprint is documented in `.gitleaksignore` so that every *other* finding
still fails the build.

If you forked or cloned this repository before 2025-09-04, those keys are still
in your copy's history. They are dead, but you may prefer not to carry them.

## Hardening checklist for a real deployment

- [ ] `USE_LOGIN=true` with a strong, unique `USER_PASSWORD`.
- [ ] `MCP_ALLOW_TOOL_EDIT=false`, with `config.json` managed as code.
- [ ] `MCP_ALLOWED_COMMANDS` narrowed to what you actually use.
- [ ] Port bound to loopback, with a TLS-terminating reverse proxy and real
      authentication in front of it.
- [ ] The container's egress restricted to the endpoints your tools need.
- [ ] `LANGSMITH_TRACING` left off unless you intend to send every prompt, tool
      result and model response to a third party.
- [ ] Every MCP server you register read and understood. A registered server
      runs with the container's full privileges.

## Supported versions

Only the tip of `main` is supported. There are no backports.
