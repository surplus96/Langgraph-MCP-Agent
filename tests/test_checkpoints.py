"""Durable conversation state.

The claim under test is not "a SQLite file appears" — it is that a conversation
written by one process is readable by the next one. Every test here that matters
opens a second checkpointer against the same path, because that is the only
thing a user actually notices.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from mcp_agent.checkpoints import (
    DISABLED,
    checkpoint_path,
    delete_thread,
    load_history,
    open_checkpointer,
)


def test_path_defaults_next_to_the_config(monkeypatch):
    monkeypatch.delenv("CHECKPOINT_DB_PATH", raising=False)
    assert checkpoint_path() == "data/checkpoints.db"


def test_path_is_configurable(monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB_PATH", "/somewhere/else.db")
    assert checkpoint_path() == "/somewhere/else.db"


def test_memory_can_be_asked_for_explicitly(monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB_PATH", DISABLED)
    assert isinstance(asyncio.run(open_checkpointer()), InMemorySaver)


def test_a_durable_checkpointer_is_the_default(monkeypatch, tmp_path):
    """The whole point. An InMemorySaver here means persistence silently died."""
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.db"))
    assert not isinstance(asyncio.run(open_checkpointer()), InMemorySaver)


def test_missing_directories_are_created(monkeypatch, tmp_path):
    """`data/` does not exist in a fresh checkout."""
    target = tmp_path / "nested" / "deeper" / "checkpoints.db"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(target))
    asyncio.run(open_checkpointer())
    assert target.exists()


def test_an_unwritable_path_degrades_rather_than_failing(monkeypatch, tmp_path):
    """A read-only volume should cost persistence, not the application.

    Verified by pointing the database at a path whose parent is a *file*, which
    is the same class of failure as a read-only mount without needing root.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(blocker / "checkpoints.db"))

    assert isinstance(asyncio.run(open_checkpointer()), InMemorySaver)


# --- The actual guarantee -----------------------------------------------------


def _write_turn(path: str, thread_id: str, messages: list) -> None:
    """Persist one turn and close the connection, as a process exit would."""

    async def run() -> None:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        connection = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        await saver.aput(
            RunnableConfig(configurable={"thread_id": thread_id, "checkpoint_ns": ""}),
            {
                "v": 1,
                "id": "checkpoint-1",
                "ts": "2026-09-07T00:00:00+00:00",
                "channel_values": {"messages": messages},
                "channel_versions": {"messages": 1},
                "versions_seen": {},
            },
            {"source": "loop", "step": 1, "parents": {}},
            {"messages": 1},
        )
        await connection.close()

    asyncio.run(run())


def _read_history(path: str, thread_id: str) -> list[dict]:
    """Open a *second* checkpointer, as the next process would."""

    async def run() -> list[dict]:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        connection = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        history = await load_history(saver, thread_id)
        await connection.close()
        return history

    return asyncio.run(run())


def test_a_conversation_survives_the_process(tmp_path):
    """Written by one connection, read by another. This is the feature."""
    path = str(tmp_path / "checkpoints.db")
    _write_turn(
        path,
        "thread-abc",
        [HumanMessage(content="what time is it"), AIMessage(content="Half past four.")],
    )

    assert _read_history(path, "thread-abc") == [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "Half past four."},
    ]


def test_an_unknown_thread_is_empty_rather_than_an_error(tmp_path):
    """A first visit, or a cleared database. Not a failure."""
    path = str(tmp_path / "checkpoints.db")
    _write_turn(path, "thread-abc", [HumanMessage(content="hello")])
    assert _read_history(path, "thread-never-used") == []


def test_threads_do_not_leak_into_each_other(tmp_path):
    path = str(tmp_path / "checkpoints.db")
    _write_turn(path, "thread-one", [HumanMessage(content="first conversation")])
    _write_turn(path, "thread-two", [HumanMessage(content="second conversation")])

    assert _read_history(path, "thread-one") == [{"role": "user", "content": "first conversation"}]
    assert _read_history(path, "thread-two") == [{"role": "user", "content": "second conversation"}]


def test_tool_and_system_messages_stay_out_of_the_transcript(tmp_path):
    """They are context for the model, not something the user ever saw."""
    path = str(tmp_path / "checkpoints.db")
    _write_turn(
        path,
        "thread-tools",
        [
            SystemMessage(content="You are an agent."),
            HumanMessage(content="what time is it"),
            ToolMessage(content='{"time": "16:30"}', tool_call_id="call-1"),
            AIMessage(content="Half past four."),
        ],
    )

    assert _read_history(path, "thread-tools") == [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "Half past four."},
    ]


def test_a_tool_only_turn_does_not_become_an_empty_bubble(tmp_path):
    """An assistant turn that only called a tool has no text of its own."""
    path = str(tmp_path / "checkpoints.db")
    _write_turn(
        path,
        "thread-empty",
        [
            HumanMessage(content="what time is it"),
            AIMessage(content=""),
            AIMessage(content="Half past four."),
        ],
    )

    assert _read_history(path, "thread-empty") == [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "Half past four."},
    ]


def test_block_structured_content_is_flattened(tmp_path):
    """Anthropic replies arrive as blocks; only the text belongs in a bubble."""
    path = str(tmp_path / "checkpoints.db")
    _write_turn(
        path,
        "thread-blocks",
        [
            HumanMessage(content="what time is it"),
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "the user wants the time"},
                    {"type": "text", "text": "Half past "},
                    {"type": "tool_use", "id": "t1", "name": "clock", "input": {}},
                    {"type": "text", "text": "four."},
                ]
            ),
        ],
    )

    assert _read_history(path, "thread-blocks") == [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "Half past four."},
    ]


def test_a_broken_checkpointer_returns_nothing_rather_than_raising():
    """A missing transcript must not take the page down with it."""

    class Exploding:
        async def aget_tuple(self, config):
            raise RuntimeError("database is locked")

    assert asyncio.run(load_history(Exploding(), "thread-abc")) == []


def test_selection_is_by_block_type_not_by_the_presence_of_a_text_key(tmp_path):
    """Pins the filter itself, which the block test above does not.

    Every block kind in that test happens to lack a `text` key, so dropping the
    `type == "text"` check changed nothing and the suite stayed green. A block
    that carries text under a non-text type is what tells the two apart — and
    the rule is that the transcript shows text blocks, not anything with a text
    key in it.
    """
    path = str(tmp_path / "checkpoints.db")
    _write_turn(
        path,
        "thread-typed",
        [
            HumanMessage(content="what time is it"),
            AIMessage(
                content=[
                    {"type": "tool_use", "name": "clock", "text": "internal detail"},
                    {"type": "text", "text": "Half past four."},
                ]
            ),
        ],
    )

    assert _read_history(path, "thread-typed") == [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "Half past four."},
    ]


# --- Deleting a conversation --------------------------------------------------


def _delete(path: str, thread_id: str) -> bool:
    """Delete through a fresh connection, as the running app would."""

    async def run() -> bool:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        connection = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        deleted = await delete_thread(saver, thread_id)
        await connection.close()
        return deleted

    return asyncio.run(run())


def test_deleting_a_thread_actually_removes_it(tmp_path):
    """Resetting has to reach storage, or the file only ever grows."""
    path = str(tmp_path / "checkpoints.db")
    _write_turn(path, "thread-doomed", [HumanMessage(content="forget this")])
    assert _read_history(path, "thread-doomed")  # precondition

    assert _delete(path, "thread-doomed") is True
    assert _read_history(path, "thread-doomed") == []


def test_deleting_one_thread_leaves_the_others_alone(tmp_path):
    """The obvious way to get this wrong is to clear the whole table."""
    path = str(tmp_path / "checkpoints.db")
    _write_turn(path, "thread-keep", [HumanMessage(content="keep this")])
    _write_turn(path, "thread-drop", [HumanMessage(content="drop this")])

    _delete(path, "thread-drop")

    assert _read_history(path, "thread-keep") == [{"role": "user", "content": "keep this"}]
    assert _read_history(path, "thread-drop") == []


def test_deleting_an_unknown_thread_is_not_an_error(tmp_path):
    """Resetting a conversation that was never stored is normal."""
    path = str(tmp_path / "checkpoints.db")
    _write_turn(path, "thread-real", [HumanMessage(content="hello")])
    assert _delete(path, "thread-never-existed") is True


def test_deleting_without_a_thread_id_does_nothing():
    """Observed by a flag, not by raising.

    An earlier version signalled "should not have been called" with an
    AssertionError, which `delete_thread` catches like any other exception — so
    removing the guard produced the same False and the suite stayed green.
    """

    class Recording:
        called = False

        async def adelete_thread(self, thread_id):
            self.called = True

    checkpointer = Recording()
    assert asyncio.run(delete_thread(checkpointer, "")) is False
    assert not checkpointer.called, "an empty thread id reached the checkpointer"


def test_a_failed_delete_is_reported_rather_than_raised():
    """Tidying up must never stop someone starting a new conversation."""

    class Exploding:
        async def adelete_thread(self, thread_id):
            raise RuntimeError("database is locked")

    assert asyncio.run(delete_thread(Exploding(), "thread-abc")) is False
