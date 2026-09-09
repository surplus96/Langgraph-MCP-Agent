"""Stopping a command so a person can look at it.

The design for 0.5.0 said `Turn` would gain a third streamed event kind for
this. Measured against a real graph, that is not how it presents: the stream
ends normally and the interrupt is only visible afterwards, in the checkpointed
state. A synthesised "event" would have been an event in name only, so the
pending approval rides on :class:`QueryResult` instead and the page renders it
where it actually arrives.

Two things make this work at all, and both were checked by running them rather
than read:

- **A rejection comes back as a ``ToolMessage``.** The middleware writes one,
  so the tool_use/tool_result pair closes and the conversation stays usable.
  This is the same invariant the per-call timeout and the shell allowlist both
  exist to protect, reached from a third direction.
- **The interrupt is in the checkpoint**, not in memory. 0.4.0 made checkpoints
  durable, which is what lets a pending approval survive a browser reload — and
  is why approvals could not have shipped before it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mcp_agent.profiles import Profile, is_allowed, needs_approval

logger = logging.getLogger(__name__)

#: What the model is told when a person declines without saying why.
DEFAULT_REJECTION = "The user did not approve that command."


@dataclass(frozen=True)
class PendingApproval:
    """One action stopped in front of a person, as the page needs to show it."""

    tool_name: str
    #: The shell command, when the action is a shell call. Empty otherwise, so
    #: the page can fall back to rendering `args`.
    command: str = ""
    args: dict[str, Any] = field(default_factory=dict)


def approve() -> dict[str, Any]:
    """The decision that lets the command run."""
    return {"type": "approve"}


def reject(message: str = "") -> dict[str, Any]:
    """The decision that does not.

    The message is passed to the model as the user's reason. Without one the
    middleware tells it the call was not executed and not to retry unchanged,
    which is the right default: silence would invite the same command again.
    """
    return {"type": "reject", "message": message.strip() or DEFAULT_REJECTION}


def build_approval_middleware(profile: Profile) -> list[Any]:
    """The interrupt gate for this profile, or nothing.

    Per command prefix rather than per tool. "May this agent use a shell" is
    not a question anyone can answer once and usefully — `git status` and
    `git push` are the same tool and not remotely the same decision — so the
    predicate reads the command, and reads *every* command on a chained line.
    """
    if not profile.shell.approve:
        return []

    from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig

    from mcp_agent.shell import SHELL_TOOL_NAME

    settings = profile.shell

    def when(request: Any) -> bool:
        """Interrupt only for a command that both matters and could run.

        The `and is_allowed` half is not redundant, and list order cannot
        replace it. `HumanInTheLoopMiddleware` hooks `after_model`, while the
        allowlist guard is a `wrap_tool_call` — different phases of the graph,
        so the interrupt happens first no matter which order they are listed
        in. Measured, not reasoned: a test written to prove the ordering
        failed against correct code, which is how this was found.

        Without it a person is stopped, reads the command, approves it, and it
        is refused anyway — teaching them their approval is decorative, which
        is the one thing an approval gate must not teach.
        """
        command = request.tool_call.get("args", {}).get("command")
        if not command:
            return False
        return is_allowed(command, settings) and needs_approval(command, settings)

    return [
        HumanInTheLoopMiddleware(
            interrupt_on={
                SHELL_TOOL_NAME: InterruptOnConfig(
                    allowed_decisions=["approve", "reject"],
                    description="This command needs your approval before it runs.",
                    when=when,
                )
            }
        )
    ]


def pending_from_state(snapshot: Any) -> PendingApproval | None:
    """Read a stopped action out of the checkpointed graph state.

    Returns None when nothing is waiting, which is the ordinary case and not a
    failure. Anything unexpected in the interrupt payload is logged and treated
    as nothing waiting: a turn that ended cleanly must not be reported as
    blocked because this could not parse it.
    """
    interrupts = getattr(snapshot, "interrupts", ()) or ()
    for interrupt in interrupts:
        value = getattr(interrupt, "value", None)
        if not isinstance(value, dict):
            continue
        requests = value.get("action_requests") or []
        if not requests:
            continue

        first = requests[0]
        if not isinstance(first, dict):
            continue

        args = first.get("args") or {}
        command = args.get("command") if isinstance(args, dict) else None
        return PendingApproval(
            tool_name=str(first.get("name", "")),
            command=str(command or ""),
            args=dict(args) if isinstance(args, dict) else {},
        )

    if interrupts:
        logger.warning("The graph is interrupted but no action request could be read from it")
    return None
