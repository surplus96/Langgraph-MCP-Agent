"""Loading, validating and persisting the MCP server configuration.

These functions are deliberately free of Streamlit imports so they can be unit
tested and reused outside a script run. They raise; the UI layer decides how to
present the failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

#: Commands an MCP server entry is allowed to launch. Registering a server spawns
#: a subprocess, so an unconstrained ``command`` field is arbitrary code
#: execution by whoever can reach the UI. Override with MCP_ALLOWED_COMMANDS.
DEFAULT_ALLOWED_COMMANDS = ("npx", "uvx", "node", "python", "python3", "docker")


class ConfigError(Exception):
    """Raised when the MCP configuration cannot be read, parsed or validated."""


class MCPServerConfig(TypedDict):
    """One MCP server entry. Either ``command``+``args`` or ``url`` is required."""

    transport: Literal["stdio", "sse", "streamable_http"]
    command: NotRequired[str]
    args: NotRequired[list[str]]
    url: NotRequired[str]
    env: NotRequired[dict[str, str]]


#: Shipped default. Deliberately empty: the previous default registered a shell
#: tool (desktop-commander) alongside a web-search tool, which is an indirect
#: prompt-injection path to credential exfiltration, and it referenced two
#: server scripts that do not exist in this repository.
DEFAULT_CONFIG: dict[str, Any] = {}


def config_path() -> Path:
    """Where the MCP config lives.

    Configurable so a container can point it at a mounted volume; the previous
    hardcoded relative path meant every tool a user added was lost when the
    container was recreated.
    """
    return Path(os.environ.get("MCP_CONFIG_PATH", "config.json"))


def allowed_commands() -> tuple[str, ...]:
    raw = os.environ.get("MCP_ALLOWED_COMMANDS")
    if not raw:
        return DEFAULT_ALLOWED_COMMANDS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def validate_server_config(name: str, config: dict[str, Any]) -> MCPServerConfig:
    """Normalise and check a single MCP server entry.

    Infers ``transport`` when it is absent, then enforces the shape the adapter
    requires and the command allowlist.

    Raises:
        ConfigError: with a message suitable for display to the user.
    """
    if not isinstance(config, dict):
        raise ConfigError(f"'{name}' must be a JSON object.")

    entry = dict(config)

    if "url" in entry:
        entry.setdefault("transport", "sse")
    else:
        entry.setdefault("transport", "stdio")

    if "command" not in entry and "url" not in entry:
        raise ConfigError(f"'{name}' requires either a 'command' or a 'url' field.")

    if "command" in entry:
        if "args" not in entry:
            raise ConfigError(f"'{name}' requires an 'args' field.")
        if not isinstance(entry["args"], list):
            raise ConfigError(f"'{name}' field 'args' must be an array.")

        command = entry["command"]
        permitted = allowed_commands()
        if command not in permitted:
            raise ConfigError(
                f"'{name}' command {command!r} is not permitted. "
                f"Allowed: {', '.join(permitted)}. "
                "Set MCP_ALLOWED_COMMANDS to change this."
            )

    return entry  # type: ignore[return-value]


def validate_config(config: dict[str, Any]) -> dict[str, MCPServerConfig]:
    """Validate every server entry, raising on the first problem."""
    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a JSON object.")
    return {name: validate_server_config(name, entry) for name, entry in config.items()}


def load_config() -> dict[str, Any]:
    """Read the config file.

    A missing file yields the default. A file that exists but cannot be parsed
    raises, rather than silently falling back to defaults — the old behaviour
    let one stray comma destroy the user's entire server list on the next save.
    """
    path = config_path()
    if not path.exists():
        return dict(DEFAULT_CONFIG)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError(f"{path} must contain a JSON object.")
    return parsed


def save_config(config: dict[str, Any]) -> None:
    """Write the config atomically, so a crash mid-write cannot truncate it."""
    path = config_path()
    tmp = path.with_suffix(path.suffix + ".tmp")

    try:
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ConfigError(f"Could not write {path}: {exc}") from exc
