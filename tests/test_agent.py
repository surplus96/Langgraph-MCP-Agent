"""Chunk parsing — the part that used to read only content[0]."""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.messages.ai import AIMessageChunk
from langchain_core.messages.tool import ToolMessage

from mcp_agent.agent import QueryResult, StreamAccumulator


@dataclass
class FakeRenderer:
    texts: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def on_text(self, text: str) -> None:
        self.texts.append(text)

    def on_tool(self, tool_log: str) -> None:
        self.tools.append(tool_log)


def feed(acc: StreamAccumulator, content) -> None:
    acc({"node": "agent", "content": AIMessageChunk(content=content)})


def test_string_content_accumulates():
    acc = StreamAccumulator(FakeRenderer())
    feed(acc, "Hello ")
    feed(acc, "world")
    assert acc.text == "Hello world"


def test_all_text_blocks_are_kept_not_just_the_first():
    acc = StreamAccumulator(FakeRenderer())
    feed(acc, [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}])
    assert acc.text == "AB"


def test_tool_use_alongside_text_in_one_chunk():
    acc = StreamAccumulator(FakeRenderer())
    feed(
        acc,
        [
            {"type": "text", "text": "calling"},
            {"type": "tool_use", "name": "search", "input": {"q": "x"}},
        ],
    )
    assert acc.text == "calling"
    assert "search" in acc.tool_log


def test_unknown_block_types_are_ignored_without_raising():
    acc = StreamAccumulator(FakeRenderer())
    feed(acc, [{"type": "some_future_block"}, {"type": "text", "text": "ok"}])
    assert acc.text == "ok"


def test_malformed_blocks_do_not_raise():
    acc = StreamAccumulator(FakeRenderer())
    feed(acc, ["not-a-dict", {"no_type_key": 1}])
    assert acc.text == ""


def test_tool_message_is_logged():
    acc = StreamAccumulator(FakeRenderer())
    acc({"node": "tools", "content": ToolMessage(content="42", tool_call_id="t1")})
    assert "42" in acc.tool_log


def test_tool_log_is_real_json_not_a_python_repr():
    acc = StreamAccumulator(FakeRenderer())
    feed(acc, [{"type": "tool_use", "name": "s", "input": {"q": "x"}}])
    assert "'" not in acc.tool_log
    assert '"name": "s"' in acc.tool_log


def test_renderer_is_called_as_content_arrives():
    renderer = FakeRenderer()
    acc = StreamAccumulator(renderer)
    feed(acc, "one")
    feed(acc, "two")
    assert renderer.texts == ["one", "onetwo"]


def test_non_message_content_is_ignored():
    acc = StreamAccumulator(FakeRenderer())
    acc({"node": "agent", "content": None})
    assert acc.text == ""


def test_query_result_defaults_to_success():
    assert QueryResult(text="hi").error is None
