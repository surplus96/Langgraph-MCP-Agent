"""Stopping a command for a person, and picking the turn back up.

Driven against a real `create_agent` graph with a scripted model rather than a
stub of the middleware: what an interrupt looks like, and that a rejection
comes back as a ToolMessage, are claims about LangGraph's behaviour, and a stub
of LangGraph would pass whether or not they hold.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from mcp_agent.agent import ChunkRenderer, resume_query, run_query
from mcp_agent.approvals import (
    DEFAULT_REJECTION,
    PendingApproval,
    approve,
    build_approval_middleware,
    pending_from_state,
    reject,
)
from mcp_agent.profiles import Profile, ShellSettings
from tests.fakes import ScriptedModel

ran: list[str] = []


@tool
def shell(command: str) -> str:
    """Run a shell command."""
    ran.append(command)
    return f"ran {command}"


class Silent:
    """A ChunkRenderer that keeps what it was given and draws nothing."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.tools: list[str] = []

    def on_text(self, text: str) -> None:
        self.texts.append(text)

    def on_tool(self, tool_log: str) -> None:
        self.tools.append(tool_log)


def _profile(approve_rules=("git push",)) -> Profile:
    return Profile(
        name="p",
        shell=ShellSettings(enabled=True, allow=("git", "ls"), approve=tuple(approve_rules)),
    )


def _agent(profile: Profile, command: str = "git push origin main"):
    from langchain.agents import create_agent

    model = ScriptedModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[{"name": "shell", "args": {"command": command}, "id": "call-1"}],
            ),
            AIMessage(content="Done."),
        ]
    )
    return create_agent(
        model,
        [shell],
        checkpointer=InMemorySaver(),
        middleware=build_approval_middleware(profile),
    )


@pytest.fixture(autouse=True)
def _clear():
    ran.clear()
    yield
    ran.clear()


def _turn(agent, thread: str, renderer: ChunkRenderer):
    return asyncio.run(run_query(agent, "push it", renderer, thread_id=thread, recursion_limit=10))


def _resume(agent, thread: str, decision, renderer: ChunkRenderer):
    return asyncio.run(
        resume_query(agent, decision, renderer, thread_id=thread, recursion_limit=10)
    )


# --- Stopping ------------------------------------------------------------------


def test_a_matching_command_stops_before_it_runs():
    agent = _agent(_profile())

    result = _turn(agent, "t1", Silent())

    assert result.awaiting_approval, result
    assert result.pending_approval.command == "git push origin main"
    assert result.pending_approval.tool_name == "shell"
    assert ran == [], "the command ran before anyone approved it"


def test_a_command_that_matches_nothing_runs_straight_through():
    agent = _agent(_profile(), command="git status")

    result = _turn(agent, "t2", Silent())

    assert not result.awaiting_approval, result
    assert ran == ["git status"]


def test_a_profile_with_no_approval_rules_builds_no_gate():
    assert build_approval_middleware(Profile(name="p")) == []


def test_stopping_is_not_reported_as_a_silent_turn():
    """A turn that stops at its first tool call has produced nothing yet.

    The empty-output rule would call that "the agent produced no output", which
    is both wrong and impossible to act on — the right answer is on screen
    waiting for a click.
    """
    result = _turn(_agent(_profile()), "t3", Silent())

    assert result.error is None, result.error
    assert result.awaiting_approval


# --- Resuming ------------------------------------------------------------------


def test_approving_runs_the_command_and_finishes_the_turn():
    agent = _agent(_profile())
    _turn(agent, "t4", Silent())

    result = _resume(agent, "t4", approve(), Silent())

    assert ran == ["git push origin main"]
    assert result.text == "Done."
    assert not result.awaiting_approval


def test_rejecting_does_not_run_it_and_still_finishes_the_turn():
    agent = _agent(_profile())
    _turn(agent, "t5", Silent())

    result = _resume(agent, "t5", reject("Not on main."), Silent())

    assert ran == [], "a rejected command ran anyway"
    assert result.text == "Done."
    assert result.error is None


def test_a_rejection_reaches_the_model_as_a_tool_result():
    """The invariant this whole feature could break.

    A refusal that does not close the tool_use/tool_result pair leaves the
    thread checkpointed with a call and no result, which Anthropic rejects on
    every later turn — the failure 0.4.1 fixed for timeouts and 0.5.0's
    allowlist avoids by refusing rather than raising. Asserted on the messages
    in the checkpoint, not on the turn's return value.
    """
    agent = _agent(_profile())
    _turn(agent, "t6", Silent())
    _resume(agent, "t6", reject("Not on main."), Silent())

    state = asyncio.run(agent.aget_state({"configurable": {"thread_id": "t6"}}))
    messages = state.values["messages"]

    tool_results = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_results) == 1, [type(m).__name__ for m in messages]
    assert "Not on main." in tool_results[0].content

    calls = [m for m in messages if getattr(m, "tool_calls", None)]
    assert len(calls) == len(tool_results), "a tool call was left without a result"


def test_the_reason_a_person_gave_is_passed_on():
    agent = _agent(_profile())
    _turn(agent, "t7", Silent())
    _resume(agent, "t7", reject("Wrong branch."), Silent())

    state = asyncio.run(agent.aget_state({"configurable": {"thread_id": "t7"}}))
    result = [m for m in state.values["messages"] if isinstance(m, ToolMessage)][0]
    assert "Wrong branch." in result.content


def test_rejecting_without_a_reason_still_tells_the_model_something():
    """Silence would invite the model to try the same command again."""
    assert reject()["message"] == DEFAULT_REJECTION
    assert reject("   ")["message"] == DEFAULT_REJECTION


def test_the_two_decisions_are_not_the_same_shape():
    assert approve() == {"type": "approve"}
    assert reject("no")["type"] == "reject"


# --- Surviving a reload ---------------------------------------------------------


def test_a_pending_approval_outlives_the_page(tmp_path):
    """0.4.0 made checkpoints durable; this is what that bought.

    A second checkpointer against the same file, as the next process would
    open, still finds the stopped command. Without durable checkpoints an
    approval could not have shipped at all — a reload would strand the graph.
    """
    import aiosqlite
    from langchain.agents import create_agent
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = str(tmp_path / "checkpoints.db")

    def build(saver):
        model = ScriptedModel(
            replies=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "shell", "args": {"command": "git push"}, "id": "call-1"}],
                ),
                AIMessage(content="Done."),
            ]
        )
        return create_agent(
            model, [shell], checkpointer=saver, middleware=build_approval_middleware(_profile())
        )

    async def stop_it() -> None:
        connection = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        await run_query(build(saver), "push it", Silent(), thread_id="durable", recursion_limit=10)
        await connection.close()

    async def read_it_back():
        connection = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        snapshot = await build(saver).aget_state({"configurable": {"thread_id": "durable"}})
        pending = pending_from_state(snapshot)
        await connection.close()
        return pending

    asyncio.run(stop_it())
    assert asyncio.run(read_it_back()) == PendingApproval(
        tool_name="shell", command="git push", args={"command": "git push"}
    )


# --- Reading the interrupt ------------------------------------------------------


def test_nothing_waiting_reads_as_nothing_waiting():
    class Snapshot:
        interrupts = ()

    assert pending_from_state(Snapshot()) is None


def test_an_unreadable_interrupt_is_not_mistaken_for_a_stopped_command():
    """A turn that finished must not be reported as blocked by a parse failure."""

    class Interrupt:
        value = "not a dict"

    class Snapshot:
        interrupts = (Interrupt(),)

    assert pending_from_state(Snapshot()) is None


def test_a_snapshot_without_the_attribute_is_survivable():
    assert pending_from_state(object()) is None


# --- The gate and the allowlist, in order ---------------------------------------


def test_a_command_the_allowlist_refuses_never_reaches_a_person():
    """Asking about a decision that cannot matter is worse than not asking.

    The allowlist decides whether a command may run at all, so it has to come
    first. Reversed, a person is stopped, reads the command, approves it — and
    it is refused anyway. That teaches them their approval is decorative, which
    is the one lesson an approval gate must not teach.
    """
    from langchain.agents import create_agent

    from mcp_agent.shell import build_allowlist_guard

    # `rm` needs approval and is not on the allowlist: only the order decides
    # which of the two answers the user gets.
    profile = Profile(
        name="p",
        shell=ShellSettings(enabled=True, allow=("git",), approve=("rm",)),
    )
    model = ScriptedModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[{"name": "shell", "args": {"command": "rm -rf build"}, "id": "c1"}],
            ),
            AIMessage(content="I could not."),
        ]
    )
    agent = create_agent(
        model,
        [shell],
        checkpointer=InMemorySaver(),
        middleware=[build_allowlist_guard(profile), *build_approval_middleware(profile)],
    )

    result = asyncio.run(
        run_query(agent, "clean up", Silent(), thread_id="order", recursion_limit=10)
    )

    assert not result.awaiting_approval, "a refused command was put to a person anyway"
    assert ran == [], "a refused command ran"

    state = asyncio.run(agent.aget_state({"configurable": {"thread_id": "order"}}))
    refusals = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    assert refusals and "does not permit" in refusals[0].content, refusals


def test_an_allowed_command_still_stops_for_approval():
    """The companion: ordering the guard first must not swallow the gate."""
    from langchain.agents import create_agent

    from mcp_agent.shell import build_allowlist_guard

    profile = Profile(
        name="p",
        shell=ShellSettings(enabled=True, allow=("git",), approve=("git push",)),
    )
    model = ScriptedModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[{"name": "shell", "args": {"command": "git push"}, "id": "c1"}],
            ),
            AIMessage(content="Done."),
        ]
    )
    agent = create_agent(
        model,
        [shell],
        checkpointer=InMemorySaver(),
        middleware=[build_allowlist_guard(profile), *build_approval_middleware(profile)],
    )

    result = asyncio.run(run_query(agent, "push", Silent(), thread_id="both", recursion_limit=10))

    assert result.awaiting_approval, result
    assert ran == []


def test_a_second_command_needing_approval_stops_again():
    """One decision approves one command, not the rest of the turn.

    The pending approval is read back out of the graph after every pass rather
    than cleared on the click, so a turn that wants two consequential commands
    asks twice.
    """
    from langchain.agents import create_agent

    profile = _profile(approve_rules=("git push", "git reset"))
    model = ScriptedModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[{"name": "shell", "args": {"command": "git push"}, "id": "c1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "shell", "args": {"command": "git reset"}, "id": "c2"}],
            ),
            AIMessage(content="Both done."),
        ]
    )
    agent = create_agent(
        model, [shell], checkpointer=InMemorySaver(), middleware=build_approval_middleware(profile)
    )

    first = _turn(agent, "twice", Silent())
    assert first.pending_approval.command == "git push"

    second = _resume(agent, "twice", approve(), Silent())
    assert second.awaiting_approval, "the second command ran without being asked about"
    assert second.pending_approval.command == "git reset"
    assert ran == ["git push"]

    third = _resume(agent, "twice", approve(), Silent())
    assert not third.awaiting_approval
    assert ran == ["git push", "git reset"]
