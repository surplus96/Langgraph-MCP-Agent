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
    """Streams the chunks it was given, recording how it was called."""

    def __init__(self, *chunks) -> None:
        self._chunks = chunks
        self.threads: list[str] = []
        self.configs: list[dict] = []
        self.inputs: list = []

    async def astream(self, inputs, config, stream_mode="messages"):
        self.configs.append(dict(config or {}))
        self.inputs.append(inputs)
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
    """Start the app with `agent` installed, then send one chat message.

    `timeout_seconds` is cut from the app's 120s default so that a broken
    iteration bound fails in seconds rather than hanging the suite for minutes.
    Measured: with the termination check removed, this file ran past a 300s cap
    while the same mutation fails a `Turn` test alone in under 7s.
    """
    app = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    state = app.session_state[SESSION_KEY]
    state.agent = agent
    state.session_initialized = True
    state.timeout_seconds = 2
    app.run()
    return app.chat_input[0].set_value(message).run()


def _said(app: AppTest) -> str:
    return " ".join(block.value for block in app.markdown)


def test_a_streamed_answer_reaches_the_page(monkeypatch):
    """A turn completes and its answer is on the page. This is a smoke test.

    It is *not* evidence that streaming works, though it was written as if it
    were: the turn ends in `st.rerun()`, so what this reads is the transcript
    rebuilt from `result.text`, and `StreamAccumulator` — not the event queue —
    is what joined the two chunks. Gutting the drawing code leaves this green.
    `tests/test_rendering.py` and the `Turn` section below are what hold the
    streamed path.

    Not asserted on `app.error`: `st.rerun()` discards the widget, so that
    assertion passes even when the turn drew an error.
    """
    app = run_turn(StubAgent(AIMessageChunk(content="Hello "), AIMessageChunk(content="world.")))

    assert not app.exception
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
    """The tool log survives into the replayed transcript as a code block.

    This is `render_history`, not the streaming path: swapping `st.code` for
    `st.markdown` in `draw` leaves it green, because the rerun redraws the log
    from `state.history`. Both matter — an escape is an escape in either place
    — and the streamed half is held in `tests/test_rendering.py`.
    """
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


# --- The Turn itself ----------------------------------------------------------
#
# Everything above reads the page after `st.rerun()`, which rebuilds it from
# `state.history` — from `turn.result`, not from the streamed events. Measured
# with a mutation pass: deleting the `for event in turn: draw(...)` loop from
# `app.py` left every test above green. What the events carry, and that they
# arrive at all, has to be asserted against the `Turn` directly.


def _turn(agent, *, timeout_seconds: float = 5.0, grace_seconds: float = 1.0):
    """One turn, deadlined tightly enough that a stuck one fails rather than hangs."""
    from mcp_agent.turns import Turn

    return Turn(
        agent,
        "do the thing",
        thread_id="thread-1",
        recursion_limit=25,
        timeout_seconds=timeout_seconds,
        grace_seconds=grace_seconds,
    )


def _drive(agent, **kwargs):
    """Run one turn to completion, returning its events and its outcome."""
    turn = _turn(agent, **kwargs)
    events = list(turn)
    return events, turn.result


def test_a_turn_yields_the_events_it_streams():
    """If iteration yielded nothing, `app.py` would draw nothing until rerun."""
    events, _ = _drive(StubAgent(AIMessageChunk(content="Hello "), AIMessageChunk(content="w.")))

    assert events, "the turn yielded nothing to draw"
    assert all(event.kind == "text" for event in events), events


def test_each_text_event_carries_the_whole_answer_so_far():
    """Placeholders are overwritten, not appended to.

    A delta reaching `st.markdown` would leave the page showing the last chunk
    alone. Accumulation happens upstream, and this is what depends on it.
    """
    events, _ = _drive(
        StubAgent(AIMessageChunk(content="Half past "), AIMessageChunk(content="four."))
    )

    assert events[-1].payload == "Half past four."
    assert all("Half past four.".startswith(event.payload) for event in events), events


def test_tool_output_arrives_as_its_own_kind():
    """`draw` routes on `kind`; a tool log labelled `text` lands in the answer."""
    events, _ = _drive(
        StubAgent(
            ToolMessage(content='{"time": "16:30"}', tool_call_id="call-1"),
            AIMessageChunk(content="Half past four."),
        )
    )

    tools = [event for event in events if event.kind == "tool"]
    assert tools, [(e.kind, e.payload) for e in events]
    assert "16:30" in tools[-1].payload


def test_events_are_handed_to_the_thread_that_iterates():
    """The reason this class exists. Drawing happens wherever these arrive.

    The agent runs on `mcp-agent-loop`, where a Streamlit write raises
    `NoSessionContext`; the events have to surface on the caller's thread.
    """
    seen: list[str] = []

    turn = _turn(StubAgent(AIMessageChunk(content="Hello")))
    for _ in turn:
        seen.append(threading.current_thread().name)

    assert seen, "no events were yielded"
    assert set(seen) == {threading.current_thread().name}
    assert "mcp-agent-loop" not in seen


def test_iteration_ends_when_the_agent_does():
    """And ends *promptly*, which is a separate claim from ending at all.

    The script thread is inside this loop, so every moment it keeps polling
    after the agent has finished is a moment the page is still frozen. Timed
    rather than merely asserted because an iteration that only ends on the
    deadline still produces the right events and the right result — it just
    takes the whole deadline to do it.
    """
    import time

    started = time.monotonic()
    events, result = _drive(StubAgent(AIMessageChunk(content="Hello")))
    elapsed = time.monotonic() - started

    assert events
    assert result.text == "Hello"
    assert elapsed < 2.0, f"iteration ran for {elapsed:.2f}s after a turn that was already done"


def test_the_thread_and_the_limit_reach_the_graph():
    """`thread_id` selects the conversation; hard-wiring it merges all of them.

    `recursion_limit` is the only thing stopping a looping agent, and both
    travel through the same config, so a config that is dropped or rebuilt
    loses both silently.
    """
    agent = StubAgent(AIMessageChunk(content="Hello"))
    _drive(agent)

    assert agent.configs, "the agent was never called"
    config = agent.configs[-1]
    assert config["configurable"]["thread_id"] == "thread-1"
    assert config["recursion_limit"] == 25


def test_a_blocked_loop_gives_up_instead_of_holding_the_page():
    """The deadline is the only thing between a stuck loop and a frozen browser.

    `run_query`'s own timeout cannot fire here: the stub blocks the loop
    thread, so nothing on that loop — including its timeout — runs. This is the
    case the grace period exists for, and it was unreachable until `__iter__`
    started consulting the deadline.
    """
    import time

    class BlockingAgent:
        async def astream(self, inputs, config, stream_mode="messages"):
            time.sleep(1.0)  # noqa: ASYNC251 — blocking the loop is the point
            yield AIMessageChunk(content="too late"), {"langgraph_node": "model"}

    started = time.monotonic()
    events, result = _drive(BlockingAgent(), timeout_seconds=0.05, grace_seconds=0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"iteration waited {elapsed:.2f}s for a blocked loop"
    assert events == []
    assert result.error and "did not respond" in result.error, result


def _spy_turn(monkeypatch):
    """Install a `Turn` that records how `app.py` built it and what it drew."""
    import mcp_agent.turns as turns

    record: dict = {"drawn": []}

    class SpyTurn(turns.Turn):
        def __init__(self, agent, query, **kwargs):
            record["query"] = query
            record.update(kwargs)
            super().__init__(agent, query, **kwargs)

        def __iter__(self):
            for event in super().__iter__():
                record["drawn"].append(event)
                yield event

    monkeypatch.setattr("mcp_agent.turns.Turn", SpyTurn)
    return record


def test_the_page_actually_iterates_the_turn(monkeypatch):
    """Holds the streaming loop in `app.py` itself.

    Nothing the loop draws survives into the assertions further up: the turn
    ends in `st.rerun()`, which rebuilds the page from the transcript. Measured
    with a mutation pass — deleting `for event in turn: draw(...)` left every
    other test in this file green, and the app would have stopped streaming
    entirely while still printing the finished answer.
    """
    record = _spy_turn(monkeypatch)
    run_turn(StubAgent(AIMessageChunk(content="Hello")))

    drawn = record["drawn"]
    assert [(e.kind, e.payload) for e in drawn] == [("text", "Hello")]


def test_the_page_hands_the_turn_what_the_session_is_set_to(monkeypatch):
    """The `app.py` -> `Turn` hop, which nothing else covers.

    Every one of these can be hard-wired at the call site with the rest of the
    suite green, and each is a real failure: a fixed `thread_id` merges every
    conversation into one checkpoint, a fixed `recursion_limit` ignores the
    sidebar, a dropped `timeout_seconds` unbounds the turn, and a dropped query
    asks the agent nothing at all.
    """
    record = _spy_turn(monkeypatch)
    agent = StubAgent(AIMessageChunk(content="Hello"))
    app = run_turn(agent, "what time is it")
    state = app.session_state[SESSION_KEY]

    assert record["query"] == "what time is it"
    assert record["thread_id"] == state.thread_id
    assert record["recursion_limit"] == state.recursion_limit
    assert record["timeout_seconds"] == state.timeout_seconds

    # And that the query survives the second hop, into the graph's input.
    sent = agent.inputs[-1]["messages"][-1]
    assert sent.content == "what time is it"


def test_the_turn_budget_reaches_the_agent():
    """Dropping it costs the partial-output guarantee, not just the bound.

    `run_query` owns the turn deadline for one reason: a turn cut short still
    reports the text it streamed and the tokens it spent. If `Turn` stops
    forwarding `timeout_seconds`, the only remaining bound is `Turn`'s own,
    which reports "did not respond" and throws the partial turn away.
    """
    import asyncio

    class SlowAgent:
        async def astream(self, inputs, config, stream_mode="messages"):
            yield AIMessageChunk(content="Half past "), {"langgraph_node": "model"}
            await asyncio.sleep(10)
            yield AIMessageChunk(content="four."), {"langgraph_node": "model"}

    events, result = _drive(SlowAgent(), timeout_seconds=0.2, grace_seconds=5.0)

    assert result.error and "exceeded" in result.error, result
    assert result.text == "Half past ", "the partial answer was thrown away"
    assert events and events[-1].payload == "Half past "


def test_the_grace_period_lets_the_agent_report_its_own_timeout():
    """What the grace is for, stated as the difference it makes.

    `Turn`'s clock starts when it is constructed; `run_query`'s starts when the
    loop gets round to running it. A loop that is busy at that moment — another
    session's tool call, say — puts the whole gap between them. Without the
    grace the two deadlines are nominally equal, so that gap makes `Turn` win:
    the page gets "did not respond" and the streamed text and spent tokens are
    thrown away, which is the one thing `run_query` owns its own timeout to
    prevent.

    The loop is blocked here *before* the turn starts, which is what makes the
    gap deterministic rather than a race.
    """
    import asyncio
    import time

    from mcp_agent.runtime import get_loop

    get_loop().call_soon_threadsafe(time.sleep, 1.0)

    class LateAgent:
        async def astream(self, inputs, config, stream_mode="messages"):
            yield AIMessageChunk(content="Half past "), {"langgraph_node": "model"}
            await asyncio.sleep(10)
            yield AIMessageChunk(content="four."), {"langgraph_node": "model"}

    _, result = _drive(LateAgent(), timeout_seconds=0.2, grace_seconds=3.0)

    assert result.text == "Half past ", result
    assert result.error and "exceeded" in result.error, result


def test_an_outcome_once_read_does_not_change_underneath():
    """Reading `result` first cancels the future; iterating must not re-read it.

    `app.py` always iterates before touching `result`, so this is latent — but
    an unconditional collect at the end of iteration replaces a reported
    timeout with `CancelledError: `, which is the shape of empty, reasonless
    error the whole module exists to have stopped producing.
    """
    import time

    class BlockingAgent:
        async def astream(self, inputs, config, stream_mode="messages"):
            time.sleep(1.0)  # noqa: ASYNC251 — blocking the loop is the point
            yield AIMessageChunk(content="too late"), {"langgraph_node": "model"}

    turn = _turn(BlockingAgent(), timeout_seconds=0.05, grace_seconds=0.05)
    first = turn.result
    assert first.error and "did not respond" in first.error

    list(turn)
    assert turn.result.error == first.error


def test_collecting_the_result_waits_only_what_is_left():
    """Iteration has already spent the deadline; waiting it again doubles it.

    The page is frozen for the whole of that wait, so the difference is what
    the user sits through — 0.6s here against 1.2s if `_collect` restarts the
    clock.
    """
    import time

    class BlockingAgent:
        async def astream(self, inputs, config, stream_mode="messages"):
            time.sleep(3.0)  # noqa: ASYNC251 — blocking the loop is the point
            yield AIMessageChunk(content="too late"), {"langgraph_node": "model"}

    started = time.monotonic()
    _drive(BlockingAgent(), timeout_seconds=0.5, grace_seconds=0.1)
    elapsed = time.monotonic() - started

    assert elapsed < 1.1, f"took {elapsed:.2f}s for a 0.6s deadline"


def test_a_batch_draws_the_tool_call_before_the_answer_to_it():
    """Both land in one batch when the turn outruns the drawing thread.

    The answer reads as a response to the tool call, so drawing it above the
    call inverts the conversation. Forced by letting the turn finish before
    iteration starts, which is what puts both events in the queue at once.
    """
    turn = _turn(
        StubAgent(
            ToolMessage(content='{"time": "16:30"}', tool_call_id="call-1"),
            AIMessageChunk(content="Half past four."),
        )
    )
    turn._future.result(timeout=10)

    assert [event.kind for event in turn] == ["tool", "text"]


def test_resuming_continues_the_interrupted_turn_rather_than_starting_one():
    """`Turn.resuming` is the only path back into a stopped turn.

    Measured: making it call `run_query` instead left every test that goes
    through the page green, because those install a spy in its place. The
    stopped command would have stayed in the checkpoint forever while the
    literal decision dict was sent to the model as a new question.
    """
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    from mcp_agent.approvals import approve, build_approval_middleware
    from mcp_agent.profiles import Profile, ShellSettings
    from mcp_agent.turns import Turn
    from tests.fakes import ScriptedModel

    ran: list[str] = []

    @tool
    def shell(command: str) -> str:
        """Run a shell command."""
        ran.append(command)
        return f"ran {command}"

    profile = Profile(
        name="p", shell=ShellSettings(enabled=True, allow=("git",), approve=("git push",))
    )
    agent = create_agent(
        ScriptedModel(
            replies=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "shell", "args": {"command": "git push"}, "id": "c1"}],
                ),
                AIMessage(content="Done."),
            ]
        ),
        [shell],
        checkpointer=InMemorySaver(),
        middleware=build_approval_middleware(profile),
    )

    first = Turn(agent, "push it", thread_id="tr", recursion_limit=10, timeout_seconds=10)
    list(first)
    assert first.result.awaiting_approval, first.result
    assert ran == []

    second = Turn.resuming(agent, approve(), thread_id="tr", recursion_limit=10, timeout_seconds=10)
    events = list(second)

    assert ran == ["git push"], "the approved command did not run"
    assert second.result.text == "Done."
    assert events, "the resumed turn drew nothing"
