"""Typed session state.

Replaces fifteen loose ``st.session_state`` keys that were initialized across six
sites under four different guard idioms — one of which keyed seven unrelated
fields off ``session_initialized``, so any path that set that flag without
running the block left ``agent`` and ``history`` undefined.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - importing these at runtime would pull
    # LangChain and LangGraph into a module the whole app imports first.
    from mcp_agent.agent import QueryResult
    from mcp_agent.approvals import PendingApproval

from mcp_agent.models import DEFAULT_EFFORT, DEFAULT_MODEL, Effort
from mcp_agent.profiles import DEFAULT_PROFILE
from mcp_agent.usage import TokenUsage

SESSION_KEY = "app_state"


def random_uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class AppState:
    """Everything one browser session holds."""

    authenticated: bool = False
    session_initialized: bool = False
    agent: Any = None
    tool_count: int = 0
    #: From the last successful build; 0 before any.
    prefix_tokens: int = 0
    timeout_seconds: int = 120
    recursion_limit: int = 25
    selected_model: str = DEFAULT_MODEL
    selected_effort: Effort = DEFAULT_EFFORT
    #: Which profile the agent was last built from, by name. Held rather than
    #: re-read so the sidebar can say "changed, re-apply" the way the model
    #: selector does.
    selected_profile: str = DEFAULT_PROFILE.name
    thread_id: str = field(default_factory=random_uuid)
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_mcp_config: dict[str, Any] | None = None
    #: True once the in-app editor has changed ``pending_mcp_config``. Applying
    #: settings writes the file only when this is set: the config is read from
    #: disk once per browser session, so writing unconditionally put a stale
    #: copy back over whatever the user had edited on disk in the meantime —
    #: which is the documented way to add a tool when the editor is off.
    config_dirty: bool = False
    #: Cumulative across the session. Cache hits only become visible from the
    #: second turn onward, so a per-turn number alone cannot show them.
    usage: TokenUsage = field(default_factory=TokenUsage)
    #: A command stopped in front of the user. While this is set the graph is
    #: interrupted mid-turn, so the conversation cannot accept a new message —
    #: sending one would append to a thread whose last tool call has no result.
    pending_approval: PendingApproval | None = None

    def record(self, result: QueryResult, *, user_query: str | None = None) -> None:
        """Fold one turn's outcome into the transcript and the running totals.

        A turn that stopped for approval is *unfinished*, not failed: whatever
        it streamed before stopping is kept, but no assistant bubble is closed
        over it, because the same turn continues once the decision is made.
        """
        self.usage = self.usage + result.usage
        if user_query is not None:
            self.history.append({"role": "user", "content": user_query})

        self.pending_approval = result.pending_approval
        if result.pending_approval is not None:
            if result.text or result.tool_log:
                self.history.append(
                    {
                        "role": "assistant",
                        "content": result.text,
                        "tool_log": result.tool_log,
                    }
                )
            return

        self.history.append(
            {
                "role": "assistant",
                "content": result.text or (result.error or ""),
                "tool_log": result.tool_log,
            }
        )

    @classmethod
    def get(cls) -> AppState:
        """Return this session's state, creating it on first access."""
        import streamlit as st

        if SESSION_KEY not in st.session_state:
            st.session_state[SESSION_KEY] = cls()
        return st.session_state[SESSION_KEY]

    def reset_conversation(self) -> None:
        """Start a fresh conversation thread.

        The thread id changes too, so the checkpointer's history is abandoned
        along with the displayed transcript rather than the two diverging.
        """
        self.history.clear()
        self.thread_id = random_uuid()
        self.usage = TokenUsage()
        # The stopped command belonged to the thread being abandoned. Left set,
        # it would offer to approve a call into a conversation that no longer
        # exists, and resuming would create a checkpoint under the new id with
        # nothing to resume.
        self.pending_approval = None
