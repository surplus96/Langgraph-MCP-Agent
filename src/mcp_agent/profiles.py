"""Profiles: what this agent is configured to be, as data rather than code.

A profile names the MCP servers to open, whether a shell is available and under
what policy, which commands need a human to approve them, and the ceilings on a
single run. It is the answer to "make this usable in any industry without
editing Python": a finance team, a lab and a game studio write three JSON
documents and share no code at all. A longer default system prompt cannot do
that job, because the thing that differs between them is which tools exist and
what is dangerous, not how to phrase a request.

Deliberately free of Streamlit and LangChain imports, like `config.py`, so the
rules can be tested without a script run or a model. This module decides what a
profile *means*; `agent.py` decides what to build from it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

#: Where a shell command may run. `docker` is the only default: the container
#: has no network, so the indirect-prompt-injection path that got the shipped
#: shell server removed in 0.2.0 — read a poisoned web page, exfiltrate a
#: credential — has nowhere to send anything. `host` runs the model's commands
#: as the Streamlit process itself and is never a default.
ShellPolicyName = Literal["docker", "host"]

SHELL_POLICIES: tuple[ShellPolicyName, ...] = ("docker", "host")

#: Below `DEFAULT_TOOL_TIMEOUT`, for the same reason that sits below the turn
#: budget: the inner bound has to expire first or it buys nothing.
DEFAULT_COMMAND_TIMEOUT = 30.0


class ProfileError(Exception):
    """Raised when a profile cannot be read, parsed or validated."""


@dataclass(frozen=True)
class ShellSettings:
    """Whether this profile can run commands, and under what constraints."""

    enabled: bool = False
    policy: ShellPolicyName = "docker"
    workspace_root: str | None = None
    #: Commands this profile may run at all. Empty means none: an allowlist that
    #: defaults to "everything" is not an allowlist. Matched on the first word.
    allow: tuple[str, ...] = ()
    #: Command prefixes that need a person to say yes, matched against the whole
    #: command line, so `git push` can require approval while `git status` does
    #: not. Per-prefix rather than per-tool because "may this agent use a
    #: shell" is not a question anyone can answer usefully once.
    approve: tuple[str, ...] = ()
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT


@dataclass(frozen=True)
class Limits:
    """Ceilings on one run. A runaway agent is a cost and a safety problem."""

    tool_calls_per_run: int | None = None
    model_calls_per_run: int | None = None


@dataclass(frozen=True)
class Profile:
    """One configured way of working."""

    name: str
    description: str = ""
    #: Appended to the base system prompt rather than replacing it: the base
    #: prompt carries the rules about tool use that every profile needs.
    system_prompt: str = ""
    #: Which servers from `config.json` to open. None means all of them, which
    #: is what reproduces the behaviour of every version before this one.
    mcp_servers: tuple[str, ...] | None = None
    shell: ShellSettings = field(default_factory=ShellSettings)
    limits: Limits = field(default_factory=Limits)


#: What you get with no profiles file at all: every configured MCP server, no
#: shell, no ceilings. Identical to 0.4.1's behaviour, so adding this module
#: changes nothing until someone writes a profile.
DEFAULT_PROFILE = Profile(
    name="general",
    description="Every configured MCP server, no shell.",
)


def profiles_path() -> Path:
    """Where profiles live. Beside `config.json` on the same mounted volume."""
    return Path(os.environ.get("MCP_PROFILES_PATH", "profiles.json"))


def _as_tuple(value: Any, *, field_name: str, profile: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"Profile '{profile}' field '{field_name}' must be a list of strings.")
    return tuple(value)


def _validate_shell(raw: Any, *, profile: str) -> ShellSettings:
    if not isinstance(raw, dict):
        raise ProfileError(f"Profile '{profile}' field 'shell' must be an object.")

    policy = raw.get("policy", "docker")
    if policy not in SHELL_POLICIES:
        raise ProfileError(
            f"Profile '{profile}' shell policy {policy!r} is not one of "
            f"{', '.join(SHELL_POLICIES)}."
        )

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ProfileError(f"Profile '{profile}' field 'shell.enabled' must be true or false.")

    allow = _as_tuple(raw.get("allow", []), field_name="shell.allow", profile=profile)
    approve = _as_tuple(raw.get("approve", []), field_name="shell.approve", profile=profile)

    if enabled and not allow:
        # Silence here would read as "no restrictions" and mean "no commands".
        # Either reading is a surprise, so refuse rather than pick one.
        raise ProfileError(
            f"Profile '{profile}' enables the shell with an empty 'allow' list, so no "
            "command could ever run. List the commands it may use, or set "
            "'enabled': false."
        )

    timeout = raw.get("command_timeout", DEFAULT_COMMAND_TIMEOUT)
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        raise ProfileError(
            f"Profile '{profile}' field 'shell.command_timeout' must be a positive number."
        )

    workspace_root = raw.get("workspace_root")
    if workspace_root is not None and not isinstance(workspace_root, str):
        raise ProfileError(f"Profile '{profile}' field 'shell.workspace_root' must be a string.")

    return ShellSettings(
        enabled=enabled,
        policy=policy,
        workspace_root=workspace_root,
        allow=allow,
        approve=approve,
        command_timeout=float(timeout),
    )


def _validate_limits(raw: Any, *, profile: str) -> Limits:
    if not isinstance(raw, dict):
        raise ProfileError(f"Profile '{profile}' field 'limits' must be an object.")

    values: dict[str, int | None] = {}
    for key in ("tool_calls_per_run", "model_calls_per_run"):
        value = raw.get(key)
        if value is None:
            values[key] = None
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ProfileError(f"Profile '{profile}' field 'limits.{key}' must be a positive int.")
        values[key] = value

    return Limits(**values)


def validate_profile(name: str, raw: Any) -> Profile:
    """Check one profile entry and return it typed.

    Raises:
        ProfileError: with a message suitable for showing to the user.
    """
    if not isinstance(raw, dict):
        raise ProfileError(f"Profile '{name}' must be a JSON object.")

    description = raw.get("description", "")
    system_prompt = raw.get("system_prompt", "")
    for field_name, value in (("description", description), ("system_prompt", system_prompt)):
        if not isinstance(value, str):
            raise ProfileError(f"Profile '{name}' field '{field_name}' must be a string.")

    servers = raw.get("mcp_servers")
    mcp_servers = (
        None if servers is None else _as_tuple(servers, field_name="mcp_servers", profile=name)
    )

    return Profile(
        name=name,
        description=description,
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        shell=_validate_shell(raw.get("shell", {}), profile=name),
        limits=_validate_limits(raw.get("limits", {}), profile=name),
    )


def load_profiles() -> dict[str, Profile]:
    """Read and validate every profile.

    A missing file yields the default profile alone, which is what makes this
    module invisible until someone opts in. A file that exists but is broken
    raises, for the same reason `load_config` does: silently falling back would
    run the agent under settings nobody chose.
    """
    path = profiles_path()
    if not path.exists():
        return {DEFAULT_PROFILE.name: DEFAULT_PROFILE}

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"Could not read {path}: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ProfileError(f"{path} must contain a JSON object of profiles.")
    if not parsed:
        raise ProfileError(f"{path} contains no profiles. Delete it to use the default.")

    return {name: validate_profile(name, entry) for name, entry in parsed.items()}


def servers_for(profile: Profile, config: dict[str, Any]) -> dict[str, Any]:
    """The subset of `config.json` this profile opens.

    Raises:
        ProfileError: if the profile names a server the configuration does not
            have. Skipping it silently would cost the user a tool with no
            indication of why the agent cannot do what they asked.
    """
    if profile.mcp_servers is None:
        return dict(config)

    missing = [name for name in profile.mcp_servers if name not in config]
    if missing:
        raise ProfileError(
            f"Profile '{profile.name}' names {', '.join(repr(m) for m in missing)}, which "
            f"{'is' if len(missing) == 1 else 'are'} not in the MCP configuration. "
            f"Available: {', '.join(sorted(config)) or 'none'}."
        )

    return {name: config[name] for name in profile.mcp_servers}


def needs_approval(command: str, settings: ShellSettings) -> bool:
    """Whether this command line matches one of the approval prefixes.

    Prefix, not equality: `git push --force origin main` has to match the rule
    written as `git push`. Matched on whitespace-delimited words so that
    `git pushover` does not match `git push`.
    """
    words = command.split()
    for rule in settings.approve:
        needle = rule.split()
        if words[: len(needle)] == needle:
            return True
    return False


def is_allowed(command: str, settings: ShellSettings) -> bool:
    """Whether the profile permits this command at all.

    The first word only: an allowlist of executables, like
    `MCP_ALLOWED_COMMANDS` for MCP servers. What follows is the model's
    business, which is what the approval rules and the sandbox are for.
    """
    words = command.split()
    return bool(words) and words[0] in settings.allow
