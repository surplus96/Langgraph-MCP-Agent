# LangGraph Agents + MCP

[![GitHub](https://img.shields.io/badge/GitHub-Langgraph--MCP--Agent-black?logo=github)](https://github.com/surplus96/Langgraph-MCP-Agent)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-≥3.12-blue?logo=python&logoColor=white)](https://www.python.org/)


## Project Overview

`LangChain-MCP-Adapters` is a toolkit provided by **LangChain AI** that enables AI agents to interact with external tools and data sources through the Model Context Protocol (MCP). This project provides a user-friendly interface for deploying ReAct agents that can access various data sources and APIs through MCP tools.

### Features

- **Streamlit Interface**: A user-friendly web interface for interacting with LangGraph `ReAct Agent` with MCP tools
- **Tool Management**: Add, remove, and configure MCP tools through the UI (Smithery JSON format supported). This is done dynamically without restarting the application
- **Streaming Responses**: View agent responses and tool calls in real-time
- **Conversation History**: Track and manage conversations with the agent

## MCP Architecture

The Model Context Protocol (MCP) consists of three main components:

1. **MCP Host**: Programs seeking to access data through MCP, such as Claude Desktop, IDEs, or LangChain/LangGraph.

2. **MCP Client**: A protocol client that maintains a 1:1 connection with the server, acting as an intermediary between the host and server.

3. **MCP Server**: A lightweight program that exposes specific functionalities through a standardized model context protocol, serving as the primary data source.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **Node.js 18+ and npm** — MCP servers are commonly launched with `npx`, and
  any such server fails to start without them
- An **`ANTHROPIC_API_KEY`**

## Quick Start with Docker

### Requirements

[Install Docker Desktop](https://www.docker.com/products/docker-desktop/)

### Run with Docker Compose

1. Move into the `dockers` directory. The compose file resolves `.env` relative
   to itself, so the file must live here — not in the repository root.

```bash
cd dockers
cp .env.example .env
```

2. Fill in `dockers/.env`. `ANTHROPIC_API_KEY`, `USER_ID` and `USER_PASSWORD`
   are required — Compose refuses to start without them rather than falling
   back to empty values. See [Environment variables](#environment-variables).

3. Start the container.

```bash
# Intel/AMD (x86_64)
docker compose -f docker-compose.yaml up -d

# Apple Silicon (arm64)
docker compose -f docker-compose.yaml -f docker-compose.arm64.yaml up -d
```

4. Open <http://localhost:8585>.

The port is published on `127.0.0.1` only. This application launches MCP
servers as subprocesses, so do not expose it directly — put a reverse proxy
with TLS and authentication in front of it first.

## Install Directly from Source

1. Clone the repository.

```bash
git clone https://github.com/surplus96/Langgraph-MCP-Agent.git
cd Langgraph-MCP-Agent
```

2. Install dependencies. `uv sync` installs exactly what `uv.lock` pins.

```bash
uv sync
```

3. Create a `.env` file from the example and fill it in.

```bash
cp .env.example .env
```

4. Run the app. It serves on **port 8501** by default (the Docker path
   remaps to 8585).

```bash
uv run streamlit run app.py
```

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | — | Unlocks the Claude models listed below. |
| `USE_LOGIN` | No | `false` | When `true`, gates the app behind a login form. |
| `USER_ID` | If `USE_LOGIN` | — | Login username. Must be non-empty. |
| `USER_PASSWORD` | If `USE_LOGIN` | — | Login password. Must be non-empty. |
| `MCP_ALLOW_TOOL_EDIT` | No | `false` | Enables the in-app MCP tool editor. |
| `MCP_ALLOWED_COMMANDS` | No | `npx,uvx,node,python,python3,docker` | Allowlist for MCP server `command` values. |
| `MCP_CONFIG_PATH` | No | `config.json` | Where the MCP server configuration is stored. |
| `LOG_LEVEL` | No | `INFO` | Python logging level. |
| `LANGSMITH_*` | No | tracing off | Read by the LangSmith SDK, not by this application. Enabling tracing sends every prompt, tool result and model response to a third party. |

With `USE_LOGIN=true` and either credential blank, the app refuses to render a
login form at all rather than accepting an empty submission.

### Available models

| Model | Max output tokens |
|---|---|
| `claude-opus-5` (default) | 128,000 |
| `claude-sonnet-5` | 128,000 |
| `claude-haiku-4-5-20251001` | 64,000 |

OpenAI support is currently not wired up; see `docs/UPGRADE_PLAN.md`.

## Configuring MCP tools

MCP servers are read from the file named by `MCP_CONFIG_PATH` (`config.json` by
default; `/app/data/config.json` under Docker, which is on the mounted volume so
it survives container recreation). `example_config.json` shows the format.

**This file is gitignored and must stay that way** — it holds credentials for
the servers it launches.

Registering an MCP server starts a subprocess, so the in-app editor is off by
default. Either edit the config file directly, or set `MCP_ALLOW_TOOL_EDIT=true`
if you trust everyone who can reach the UI. Server `command` values are checked
against `MCP_ALLOWED_COMMANDS` either way.

> **Do not** put a filesystem or shell MCP server in the same agent as a
> web-search or web-fetch server. A page returned by the search tool can carry
> instructions that drive the shell tool — an exfiltration path that needs no
> access to your UI at all.

Find servers at [Smithery](https://smithery.ai/). Pin exact versions rather than
using `@latest`, which fetches and executes whatever is published at run time.

## Development

```bash
uv sync              # install, including dev dependencies
uv run ruff check .  # lint
uv run ruff format . # format
uv run mypy src/mcp_agent app.py
uv run pytest -q     # 122 tests
```

## Usage

1. Start the app (see above) and open it in your browser.
2. Pick a model in the sidebar.
3. Configure MCP tools (see [Configuring MCP tools](#configuring-mcp-tools)).
4. Click **Apply Settings**. The sidebar then shows the number of tools
   discovered and the active model.
5. Ask questions in the chat. Tool calls appear in a collapsible panel under
   each answer.

Changing the model or the tool configuration requires clicking **Apply
Settings** again to rebuild the agent.

![MCP agent UI](assets/langgraph-mcp-agent-UI-01.png) 


## Documentation

| Document | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit together and why: the event loop, MCP session lifecycle, prompt caching, token accounting. |
| [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md) | The configuration file in full — every field, what is validated, troubleshooting. |
| [SECURITY.md](SECURITY.md) | Threat model, what each control does and does not do, reporting a vulnerability. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, style, and the standard a test has to meet here. |
| [CHANGELOG.md](CHANGELOG.md) | What changed in this release. |
| [docs/UPGRADE_PLAN.md](docs/UPGRADE_PLAN.md) | The modernization plan this work followed, including what was deliberately deferred. |

## License

This project is released under the MIT License. See [LICENSE](LICENSE) for the full text.

Forked from [teddylee777/langgraph-mcp-agents](https://github.com/teddylee777/langgraph-mcp-agents).

## References

- https://github.com/langchain-ai/langchain-mcp-adapters