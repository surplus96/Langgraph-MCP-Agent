"""Chunk parsing — the part that used to read only content[0]."""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.messages.ai import AIMessageChunk
from langchain_core.messages.tool import ToolMessage

from mcp_agent.agent import QueryResult, StreamAccumulator, TokenUsage


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


# --- Usage accumulation across a streamed turn --------------------------------


def usage_chunk(**details):
    """A chunk shaped like what langchain-anthropic emits during streaming."""
    return AIMessageChunk(content="", usage_metadata=details)


def test_usage_sums_across_chunks_of_one_call():
    """Streamed chunks carry incremental usage; summing must give the total."""
    acc = StreamAccumulator(FakeRenderer())
    # message_start carries the input side, message_delta the output side.
    acc(
        {
            "node": "model",
            "content": usage_chunk(
                input_tokens=1000,
                output_tokens=1,
                total_tokens=1001,
                input_token_details={"cache_read": 0, "cache_creation": 900},
            ),
        }
    )
    acc(
        {
            "node": "model",
            "content": usage_chunk(
                input_tokens=0,
                output_tokens=40,
                total_tokens=40,
                input_token_details={"cache_read": 0, "cache_creation": 0},
            ),
        }
    )
    assert acc.usage.input_tokens == 1000
    assert acc.usage.output_tokens == 41
    assert acc.usage.cache_creation == 900


def test_usage_sums_across_react_iterations():
    """A turn makes several model calls; all of them are billed, so all count."""
    acc = StreamAccumulator(FakeRenderer())
    for _ in range(3):
        acc(
            {
                "node": "model",
                "content": usage_chunk(
                    input_tokens=1000,
                    output_tokens=10,
                    total_tokens=1010,
                    input_token_details={"cache_read": 900, "cache_creation": 0},
                ),
            }
        )
    assert acc.usage.input_tokens == 3000
    assert acc.usage.cache_read == 2700
    assert acc.usage.cache_hit_rate == 0.9


def test_chunks_without_usage_do_not_disturb_the_total():
    acc = StreamAccumulator(FakeRenderer())
    feed(acc, "plain text with no usage attached")
    assert acc.usage == TokenUsage()


async def test_timeout_keeps_the_text_and_tokens_already_paid_for():
    """A cut-short turn must not discard what it already streamed and billed."""
    import asyncio

    from mcp_agent import agent as agent_module

    renderer = FakeRenderer()

    async def slow_stream(graph, inputs, callback, config=None, node_names=None):
        callback(
            {
                "node": "model",
                "content": AIMessageChunk(
                    content=[{"type": "text", "text": "partial answer"}],
                    usage_metadata={
                        "input_tokens": 900,
                        "output_tokens": 5,
                        "total_tokens": 905,
                        "input_token_details": {"cache_read": 800, "cache_creation": 0},
                    },
                ),
            }
        )
        await asyncio.sleep(10)

    original = agent_module.astream_graph
    agent_module.astream_graph = slow_stream
    try:
        result = await agent_module.run_query(
            agent="unused",
            query="hello",
            renderer=renderer,
            thread_id="t1",
            recursion_limit=10,
            timeout_seconds=0.05,
        )
    finally:
        agent_module.astream_graph = original

    assert result.error is not None
    assert result.text == "partial answer"
    assert result.usage.input_tokens == 900
    assert result.usage.cache_read == 800
