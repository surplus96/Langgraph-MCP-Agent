"""The shell capability, and the reasons it is off until someone turns it on.

0.2.0 removed the shipped shell server because a shell alongside a web-search
tool is an indirect prompt-injection path to credential exfiltration: the model
reads a page it was told to summarise, the page tells it to run a command, and
the command sends a key somewhere. That reasoning does not stop being true
because 0.5.0 makes the shell the point. So the capability is earned back with
three separate things rather than reinstated with one:

1. **It does not exist unless asked for**, twice — the operator sets
   ``MCP_ENABLE_SHELL=true`` and the profile sets ``shell.enabled``. Either
   alone gets nothing.
2. **The boundary is the sandbox**, not the allowlist. The default execution
   policy is a container with no network, a read-only root filesystem and a
   non-root user, and it may only mount a directory the *operator* named
   (``MCP_WORKSPACE_ROOT``) — a profile choosing that was found putting the
   operator's home directory inside the sandbox, writable, because
   ``--read-only`` does not cover bind mounts.

   This closes the command's route out and not every route out. A security
   review found the other one: the model's own reply renders as markdown, and
   an image embed in it is fetched by the *browser*, which has a network even
   when the container does not. That is why :func:`mcp_agent.rendering.without_images`
   exists. An earlier version of this docstring said the exfiltration step "has
   nowhere to go", which was true of the shell and not of the page, and it was
   the sentence the whole feature's justification hung on.
3. **The allowlist is defence in depth**, and it is only that. It reads every
   command on the line rather than the first, because the shell tool's own
   description tells the model to chain with ``&&``, and it refuses command
   substitution outright rather than trying to parse it.

   It cannot save a profile that lists a bare interpreter — ``python``,
   ``make``, ``pytest`` all run arbitrary code, so listing one is listing
   ``sh`` — and the shipped examples name none. The test for that reads the
   allowlist entries, because the nine-probe test that came first did not:
   ``python`` could be added to both shipped allowlists and it stayed green,
   since every probe is refused by the argument denylist regardless. But "do
   not list an interpreter" is not a rule anyone can follow by inspection:
   ``git -c core.pager='sh -c id'``, ``git clone ext::sh`` and
   ``rg --pre /bin/sh`` all run commands, and ``git`` and ``rg`` are the whole
   point of a repository profile. A review demonstrated all three against this
   project's own examples, so those argument forms are refused too — as a
   denylist, which is incomplete by construction and is not what makes any of
   this safe.

``HostExecutionPolicy`` runs the model's commands as the Streamlit process. It
is reachable, because an operator who has read the above may have a reason, and
it is never a default and never inherited.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from mcp_agent.profiles import Profile, ShellPolicyName, is_allowed

logger = logging.getLogger(__name__)

#: Image the sandbox runs in. Pinned rather than floating: `latest` would mean
#: the tools a profile's allowlist names can change under it without anything
#: in this repository changing.
#:
#: Debian-based, not Alpine. `ShellToolMiddleware` runs commands through
#: `/bin/bash`, which Alpine does not ship, so an Alpine image fails every
#: command. The failure would be safe — the session errors rather than running
#: anything — but it would push an operator debugging "the shell does not
#: work" toward MCP_SHELL_POLICY=host, which is the configuration this module
#: exists to keep exceptional. Override with MCP_SANDBOX_IMAGE.
DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"


def sandbox_image() -> str:
    """The container image commands run in. Override with MCP_SANDBOX_IMAGE.

    Operator-side, like every other decision about where a command runs: a
    profile naming its own image would be choosing what is installed alongside
    the commands it is allowed to run.
    """
    return os.environ.get("MCP_SANDBOX_IMAGE", "").strip() or DEFAULT_SANDBOX_IMAGE


def workspace_root() -> str | None:
    """The one directory a profile may mount, or None if the operator set none.

    Without this a profile — a JSON file someone may have been handed — chooses
    what is bind-mounted into the container, and `--read-only` does not apply
    to bind mounts. A security review demonstrated `workspace_root: "/root"`
    producing `docker run -v /root:/root`, which puts the operator's home
    directory inside the sandbox, writable.
    """
    return os.environ.get("MCP_WORKSPACE_ROOT", "").strip() or None


def resolve_workspace(profile: Profile) -> str | None:
    """Where this profile's commands run, once the operator's limit is applied.

    Returns None when the profile asked for nothing, when the operator set no
    root, or when the profile asked for somewhere outside it. None is the safe
    answer to "I could not honour that", but it is worth knowing what it costs:
    `ShellToolMiddleware` makes a host temporary directory and
    `DockerExecutionPolicy` then declines to mount it, because it recognises
    its own prefix — so the container runs `-w /` on a read-only root and
    commands can read the image and write nowhere. Under the host policy the
    temporary directory is the working directory and is writable. Either way
    the shell still starts, and nothing of the operator's is inside it.
    """
    wanted = profile.shell.workspace_root
    if not wanted:
        return None

    root = workspace_root()
    if root is None:
        logger.warning(
            "Profile %r asks for workspace %r but MCP_WORKSPACE_ROOT is not set; "
            "nothing will be mounted (the docker policy then runs -w / on a "
            "read-only root, so commands can write nowhere).",
            profile.name,
            wanted,
        )
        return None

    try:
        resolved = Path(wanted).resolve()
        limit = Path(root).resolve()
    except OSError as exc:  # pragma: no cover - resolution is filesystem-dependent
        logger.warning("Could not resolve workspace %r: %s", wanted, exc)
        return None

    # `is_relative_to` and not a string prefix: `/workspace-other` starts with
    # `/workspace` as text and is a different directory.
    if resolved != limit and not resolved.is_relative_to(limit):
        logger.warning(
            "Profile %r asks for workspace %s, which is outside MCP_WORKSPACE_ROOT=%s; "
            "nothing will be mounted (the docker policy then runs -w / on a "
            "read-only root, so commands can write nowhere).",
            profile.name,
            resolved,
            limit,
        )
        return None

    return str(resolved)


#: The tool `ShellToolMiddleware` registers. Named once because the allowlist
#: guard has to recognise it, so a rename upstream breaks one place, not two.
SHELL_TOOL_NAME = "shell"

#: Not root inside the container either. The read-only root filesystem already
#: stops most of what root would buy, and the two together mean a write has to
#: go to the mounted workspace or nowhere.
SANDBOX_USER = "nobody"


#: Shapes that are almost certainly a credential, redacted out of shell output
#: before the model sees it. Without these, `cat .env` puts a key into the
#: model's context, into the transcript, and permanently into
#: `data/checkpoints.db`, which is an unencrypted file on the mounted volume.
#:
#: Not a secret scanner. It catches the provider tokens this project's users
#: are most likely to have lying about, and it is the last line rather than the
#: first: the sandbox is why the file is not reachable in the first place.
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    ("openai_key", r"\bsk-[A-Za-z0-9]{20,}"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def secret_redactions() -> list[Any]:
    """The redaction rules handed to the shell tool."""
    from langchain.agents.middleware import RedactionRule

    return [
        RedactionRule(pii_type=name, strategy="redact", detector=pattern)
        for name, pattern in SECRET_PATTERNS
    ]


def shell_enabled() -> bool:
    """The operator's half of the switch. Off unless set to exactly true."""
    return os.environ.get("MCP_ENABLE_SHELL", "false").strip().lower() == "true"


def configured_policy() -> ShellPolicyName | None:
    """An operator-level override of the profile's policy, or None.

    Exists so that `host` takes saying so in the environment as well as in a
    profile: a profile is a file someone may have been handed, and running the
    model's commands as this process should not be reachable by receiving one.
    """
    raw = os.environ.get("MCP_SHELL_POLICY", "").strip().lower()
    if not raw:
        return None
    if raw not in ("docker", "host"):
        logger.warning("MCP_SHELL_POLICY=%r is not 'docker' or 'host'; ignoring it", raw)
        return None
    return raw  # type: ignore[return-value]


def resolve_policy(profile: Profile) -> ShellPolicyName:
    """Which execution policy this run uses. `host` requires two yeses.

    The environment variable *permits* the host policy; it does not impose it.
    A deployment that sets it still runs every profile that asked for the
    sandbox in the sandbox — otherwise one operator decision would silently
    move every profile onto the host, which is the opposite of what someone
    setting it is trying to express.

    A profile naming `host` on a deployment that did not opt in is downgraded
    rather than refused, and says so: refusing would take away a shell the
    profile could safely have had, and running it on the host would be the
    thing this is here to prevent.
    """
    if profile.shell.policy != "host":
        return "docker"

    if configured_policy() != "host":
        logger.warning(
            "Profile %r asks for the host shell policy; running sandboxed instead. "
            "Set MCP_SHELL_POLICY=host to allow it.",
            profile.name,
        )
        return "docker"

    return "host"


def build_execution_policy(profile: Profile) -> Any:
    """The sandbox a command runs in.

    `network_enabled=False` is the load-bearing one: it closes the second half
    of the injection path that got the shell removed in 0.2.0. The rest bounds
    what a command can do to the machine it did reach.
    """
    from langchain.agents.middleware import DockerExecutionPolicy, HostExecutionPolicy

    timeout = profile.shell.command_timeout

    if resolve_policy(profile) == "host":
        logger.warning(
            "Profile %r runs shell commands on the host, as this process. "
            "Nothing isolates them from the machine Streamlit is running on.",
            profile.name,
        )
        return HostExecutionPolicy(command_timeout=timeout)

    return DockerExecutionPolicy(
        image=sandbox_image(),
        command_timeout=timeout,
        network_enabled=False,
        read_only_rootfs=True,
        user=SANDBOX_USER,
    )


def build_allowlist_guard(profile: Profile) -> Any:
    """Middleware that refuses a command the profile does not list.

    Refused rather than raised: the model gets a `ToolMessage` saying what was
    denied and why, which keeps the tool_use/tool_result pair complete and lets
    it choose something else. Raising would strand the pair and poison the
    thread, which is the failure 0.4.1 fixed for timeouts.
    """
    from langchain.agents.middleware import wrap_tool_call
    from langchain_core.messages import ToolMessage

    settings = profile.shell
    permitted = ", ".join(sorted(settings.allow)) or "nothing"

    # Async, and that is not a style choice. `wrap_tool_call` on a sync
    # function defines only the sync hook, and this application drives the
    # graph with `astream`, so LangGraph raises NotImplementedError on every
    # shell call — the guard would never run and the turn would fail. Found by
    # a test that put a command through a real graph; the unit tests called the
    # sync hook directly and passed, which is testing the method production
    # never reaches.
    @wrap_tool_call(name="ShellAllowlistMiddleware")
    async def guard(request: Any, handler: Any) -> Any:
        if request.tool_call.get("name") != SHELL_TOOL_NAME:
            return await handler(request)

        command = request.tool_call.get("args", {}).get("command")
        if command is None:  # a restart, which runs nothing
            return await handler(request)

        if not is_allowed(command, settings):
            # The command itself is not logged. A refused line is exactly the
            # one most likely to carry something an attacker planted, and
            # refusing it should not be what writes that to disk. The first
            # word is enough to see the shape of what is being attempted.
            logger.warning(
                "Refused a shell command starting %r under profile %r",
                command.split()[0] if command.split() else "",
                profile.name,
            )
            return ToolMessage(
                content=(
                    f"Refused: this profile does not permit that command. It allows "
                    f"{permitted}, and every command on a chained line has to be "
                    f"listed. Tell the user what you wanted to run and why, rather "
                    f"than trying a variation."
                ),
                tool_call_id=request.tool_call["id"],
                name=SHELL_TOOL_NAME,
                status="error",
            )

        return await handler(request)

    return guard


def build_shell_middleware(profile: Profile) -> list[Any]:
    """The shell capability for this profile, or nothing at all.

    Both switches have to be on. The operator's is checked here rather than in
    the profile so that a profile file cannot turn on a capability the
    deployment did not enable.
    """
    if not profile.shell.enabled:
        return []
    if not shell_enabled():
        logger.info(
            "Profile %r asks for a shell but MCP_ENABLE_SHELL is not true; not building one.",
            profile.name,
        )
        return []

    from langchain.agents.middleware import ShellToolMiddleware

    policy = build_execution_policy(profile)
    shell = ShellToolMiddleware(
        workspace_root=resolve_workspace(profile),
        execution_policy=policy,
        redaction_rules=secret_redactions(),
    )

    # Checked, not assumed. `ShellToolMiddleware`'s own default when it is
    # handed no policy is `HostExecutionPolicy` — so this one line going
    # missing does not disable the sandbox, it moves every command onto the
    # host, which is the failure direction this module exists to prevent. A
    # mutation pass found the line unconstrained; the test that now covers it
    # is worth having, and so is refusing to hand back a shell that is not the
    # one we built.
    if shell._execution_policy is not policy:  # noqa: SLF001 - fail closed, not tidy
        raise RuntimeError(
            "The shell middleware did not take the execution policy it was given; "
            "refusing to run commands under an unknown policy."
        )
    # The order below is the order these read in, and it is asserted in
    # `tests/test_shell.py`. What it is *not* is the thing that keeps a refused
    # command from stopping a person: `HumanInTheLoopMiddleware` hooks
    # `after_model` while the guard is a `wrap_tool_call`, so the interrupt
    # fires first whatever this list says. That is why the approval predicate
    # in `approvals.py` carries `and is_allowed` — measured, by a test written
    # to prove the ordering and failing against correct code. Do not delete
    # that half on the strength of this line.
    from mcp_agent.approvals import build_approval_middleware

    return [build_allowlist_guard(profile), *build_approval_middleware(profile), shell]
