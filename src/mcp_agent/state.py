"""Typed session state.

Replaces fifteen loose ``st.session_state`` keys that were initialized across six
sites under four different guard idioms — one of which keyed seven unrelated
fields off ``session_initialized``, so any path that set that flag without
running the block left ``agent`` and ``history`` undefined.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp_agent.agent import TokenUsage
from mcp_agent.models import DEFAULT_MODEL

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
    timeout_seconds: int = 120
    recursion_limit: int = 25
    selected_model: str = DEFAULT_MODEL
    thread_id: str = field(default_factory=random_uuid)
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_mcp_config: dict[str, Any] | None = None
    #: Cumulative across the session. Cache hits only become visible from the
    #: second turn onward, so a per-turn number alone cannot show them.
    usage: TokenUsage = field(default_factory=TokenUsage)

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
