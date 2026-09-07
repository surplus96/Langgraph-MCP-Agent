"""A chat turn, end to end, through the real script.

No test drove a turn before this file existed, which is why a streaming path
that raised on every write shipped and stayed shipped. These tests exist to
make that class of failure impossible to miss: they run `app.py` under
`AppTest`, send a message, and read what a user would see.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from streamlit.testing.v1 import AppTest

from mcp_agent.state import SESSION_KEY

APP = str(Path(__file__).parent.parent / "app.py")
TIMEOUT = 60


class StubAgent:
    """Streams the chunks it was given, recording which thread it ran on."""

    def __init__(self, *chunks) -> None:
        self._chunks = chunks
        self.threads: list[str] = []

    async def astream(self, inputs, config, stream_mode="messages"):
        for chunk in self._chunks:
            self.threads.append(threading.current_thread().name)
            yield chunk, {"langgraph_node": "model"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import streamlit as st

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("CHECKPOINT_DB_PATH", ":memory:")
    for key in ("USE_LOGIN", "USER_ID", "USER_PASSWORD", "MCP_ALLOW_TOOL_EDIT"):
        monkeypatch.delenv(key, raising=False)
    st.cache_resource.clear()


def run_turn(agent, message: str = "do the thing") -> AppTest:
    """Start the app with `agent` installed, then send one chat message."""
    app = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    state = app.session_state[SESSION_KEY]
    state.agent = agent
    state.session_initialized = True
    app.run()
    return app.chat_input[0].set_value(message).run()


def _said(app: AppTest) -> str:
    return " ".join(block.value for block in app.markdown)


def test_a_streamed_answer_reaches_the_page(monkeypatch):
    """The whole point. This failed on every turn before the queue existed."""
    app = run_turn(StubAgent(AIMessageChunk(content="Hello "), AIMessageChunk(content="world.")))

    assert not app.exception
    assert not app.error, [e.value for e in app.error]
    assert "Hello world." in _said(app)


def test_the_answer_is_kept_in_the_transcript(monkeypatch):
    app = run_turn(StubAgent(AIMessageChunk(content="Half past four.")))

    history = app.session_state[SESSION_KEY].history
    assert history[-1] == {
        "role": "assistant",
        "content": "Half past four.",
        "tool_log": "",
    }


def test_tool_output_is_rendered_as_code(monkeypatch):
    """Not markdown: a literal fence in untrusted output must not escape."""
    app = run_turn(
        StubAgent(
            (ToolMessage(content='{"time": "16:30"}', tool_call_id="call-1")),
            AIMessageChunk(content="Half past four."),
        )
    )

    assert not app.exception
    assert any("16:30" in block.value for block in app.code), (
        f"tool output not in a code block; code blocks were {[c.value for c in app.code]}"
    )


def test_the_agent_runs_off_the_script_thread(monkeypatch):
    """The speed of the whole app depends on this staying true.

    The agent belongs on the background loop. If it ever ran inline on the
    script thread the render bug would disappear for the wrong reason, and the
    held MCP sessions would be back to being entered and exited from whichever
    thread Streamlit happened to use.
    """
    agent = StubAgent(AIMessageChunk(content="Hello"))
    run_turn(agent)

    assert agent.threads, "the agent never ran"
    assert set(agent.threads) == {"mcp-agent-loop"}, agent.threads


def test_an_agent_that_produces_nothing_says_so(monkeypatch):
    """A silent turn is a failure, not an empty bubble.

    Asserted on the transcript rather than on `app.error`: the turn ends in
    `st.rerun()`, which discards the error widget the run just drew, so what
    the user is actually left looking at is the recorded turn.
    """
    app = run_turn(StubAgent())

    assert not app.exception
    last = app.session_state[SESSION_KEY].history[-1]
    assert last["role"] == "assistant"
    assert "no output" in last["content"], last
