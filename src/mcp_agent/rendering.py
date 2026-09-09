"""Drawing one streamed event.

This is the one piece of presentation worth unit testing, so by this
repository's own rule it does not belong in ``app.py``. It is a routing
decision with two wrong answers, and both were reachable: sending text to the
tool pane, and sending untrusted tool output through live markdown. Neither is
observable from a test that drives the script, because the turn ends in
``st.rerun()`` and the page is rebuilt from the transcript by
``render_history`` — mutating either branch of this function left the whole
suite green.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from mcp_agent.turns import TurnEvent


def draw(event: TurnEvent, text_placeholder: Any, tool_placeholder: Any) -> None:
    """Draw one streamed event into its own placeholder.

    Called from the script thread only. The agent runs on the background loop
    and hands its output over as data — see :mod:`mcp_agent.turns` for why
    drawing from there raises on every write.
    """
    if event.kind == "text":
        text_placeholder.markdown(event.payload)
        return

    with tool_placeholder.expander("🔧 Tool Call Information", expanded=True):
        # st.code, not st.markdown: tool output is untrusted and a literal
        # fence inside it would otherwise escape into live markdown.
        st.code(event.payload, language="json")
