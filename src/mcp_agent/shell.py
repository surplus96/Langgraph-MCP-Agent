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
   non-root user, so the exfiltration step of that attack has nowhere to go.
3. **The allowlist is defence in depth.** It reads every command on the line
   rather than the first, because the shell tool's own description tells the
   model to chain with ``&&``.

``HostExecutionPolicy`` runs the model's commands as the Streamlit process. It
is reachable, because an operator who has read the above may have a reason, and
it is never a default and never inherited.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp_agent.profiles import Profile, ShellPolicyName, is_allowed

logger = logging.getLogger(__name__)

#: Image the sandbox runs in. Pinned rather than floating: `latest` would mean
#: the tools a profile's allowlist names can change under it without anything
#: in this repository changing.
DEFAULT_SANDBOX_IMAGE = "python:3.12-alpine3.19"

#: The tool `ShellToolMiddleware` registers. Named once because the allowlist
#: guard has to recognise it, so a rename upstream breaks one place, not two.
SHELL_TOOL_NAME = "shell"

#: Not root inside the container either. The read-only root filesystem already
#: stops most of what root would buy, and the two together mean a write has to
#: go to the mounted workspace or nowhere.
SANDBOX_USER = "nobody"


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
        image=DEFAULT_SANDBOX_IMAGE,
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

    @wrap_tool_call(name="ShellAllowlistMiddleware")
    def guard(request: Any, handler: Any) -> Any:
        if request.tool_call.get("name") != SHELL_TOOL_NAME:
            return handler(request)

        command = request.tool_call.get("args", {}).get("command")
        if command is None:  # a restart, which runs nothing
            return handler(request)

        if not is_allowed(command, settings):
            logger.warning("Refused shell command %r under profile %r", command, profile.name)
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

        return handler(request)

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

    shell = ShellToolMiddleware(
        workspace_root=profile.shell.workspace_root,
        execution_policy=build_execution_policy(profile),
    )
    # The guard is listed first so it wraps the tool the middleware registers.
    return [build_allowlist_guard(profile), shell]
