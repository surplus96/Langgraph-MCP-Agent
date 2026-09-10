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


# --- The middleware chain -------------------------------------------------------
#
# `create_agent` applies middleware as a chain, so its order is behaviour. It
# was a literal list with a comment until 0.5.0 made it depend on the profile;
# a comment cannot fail, so these can.


def _spec():
    from mcp_agent.models import DEFAULT_MODEL, MODEL_REGISTRY

    return MODEL_REGISTRY[DEFAULT_MODEL]


def _model():
    """A real chat model. Constructing one makes no network call.

    `SummarizationMiddleware` reaches for `with_retry` on whatever it is given,
    so a bare stub is not enough and faking that one method would only pin the
    stub.
    """
    import os

    from mcp_agent.models import DEFAULT_MODEL, build_model

    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")
    return build_model(DEFAULT_MODEL)


def _names(profile) -> list[str]:
    from mcp_agent.agent import build_middleware

    return [type(m).__name__ for m in build_middleware(profile, _model(), _spec())]


def test_the_default_profile_builds_the_chain_0_4_1_had():
    """Adding the profile layer must not have changed what an unconfigured app does."""
    from mcp_agent.profiles import DEFAULT_PROFILE

    assert _names(DEFAULT_PROFILE) == [
        "SummarizationMiddleware",
        "AnthropicPromptCachingMiddleware",
    ]


def test_prompt_caching_is_last():
    """It has to see the final shape of everything above it.

    Asserted on the position rather than on membership: a reorder during a
    refactor is silent, costs the cached prefix on every turn, and shows up
    only as a bill.
    """
    from mcp_agent.profiles import Limits, Profile

    full = Profile(name="p", limits=Limits(tool_calls_per_run=10, model_calls_per_run=5))
    assert _names(full)[-1] == "AnthropicPromptCachingMiddleware"


def test_summarization_sits_after_the_ceilings_and_before_caching():
    from mcp_agent.profiles import Limits, Profile

    full = Profile(name="p", limits=Limits(tool_calls_per_run=10, model_calls_per_run=5))

    assert _names(full) == [
        "ModelCallLimitMiddleware",
        "ToolCallLimitMiddleware",
        "SummarizationMiddleware",
        "AnthropicPromptCachingMiddleware",
    ]


def test_a_profile_with_no_limits_gets_no_limit_middleware():
    """Both constructors reject being given no limit, so this is not cosmetic."""
    from mcp_agent.profiles import Profile

    assert not [name for name in _names(Profile(name="p")) if "Limit" in name]


def test_one_limit_does_not_drag_in_the_other():
    from mcp_agent.profiles import Limits, Profile

    only_tools = Profile(name="p", limits=Limits(tool_calls_per_run=10))
    assert [name for name in _names(only_tools) if "Limit" in name] == ["ToolCallLimitMiddleware"]


def test_a_limit_that_is_hit_closes_its_tool_calls():
    """`end`, not `continue` or `error`.

    Ending writes a ToolMessage for every call it cut, which keeps the
    tool_use/tool_result pair complete. `error` raises past that and leaves the
    thread checkpointed with an unmatched tool call — the failure 0.4.1 fixed
    for timeouts, reached from a different direction.
    """
    from mcp_agent.agent import build_middleware
    from mcp_agent.profiles import Limits, Profile

    chain = build_middleware(
        Profile(name="p", limits=Limits(tool_calls_per_run=10, model_calls_per_run=5)),
        _model(),
        _spec(),
    )

    for middleware in chain:
        if "Limit" in type(middleware).__name__:
            assert middleware.exit_behavior == "end", type(middleware).__name__


# --- The profile's own prompt ---------------------------------------------------


def test_a_profile_without_a_prompt_uses_the_base_one_unchanged():
    from mcp_agent.agent import SYSTEM_PROMPT, system_prompt_for
    from mcp_agent.profiles import Profile

    assert system_prompt_for(Profile(name="p")) == SYSTEM_PROMPT
    assert system_prompt_for(Profile(name="p", system_prompt="   \n  ")) == SYSTEM_PROMPT


def test_a_profile_prompt_is_appended_not_substituted():
    """The base prompt carries the tool-use rules every profile still needs."""
    from mcp_agent.agent import SYSTEM_PROMPT, system_prompt_for
    from mcp_agent.profiles import Profile

    combined = system_prompt_for(Profile(name="p", system_prompt="You are in a git repository."))

    assert combined.startswith(SYSTEM_PROMPT)
    assert combined.endswith("You are in a git repository.")
    assert len(combined) > len(SYSTEM_PROMPT)


# --- The profile actually reaches the built agent --------------------------------


def _build(profile, monkeypatch):
    import asyncio
    import os

    from langgraph.checkpoint.memory import InMemorySaver

    from mcp_agent.agent import build_agent
    from mcp_agent.models import DEFAULT_MODEL

    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")
    return asyncio.run(build_agent(DEFAULT_MODEL, [], InMemorySaver(), profile=profile))


def test_the_profile_reaches_the_middleware_chain(monkeypatch):
    """Nothing downstream observes this, so it has to be asserted at the hop.

    Measured: hard-wiring `build_middleware` to the default profile left every
    other test green, so a profile's ceilings could be silently dropped while
    its name and description still showed in the sidebar.
    """
    from mcp_agent.profiles import Limits, Profile

    seen: list = []
    real = None

    import mcp_agent.agent as agent_module

    real = agent_module.build_middleware

    def spy(profile, model, spec):
        seen.append(profile)
        return real(profile, model, spec)

    monkeypatch.setattr(agent_module, "build_middleware", spy)

    wanted = Profile(name="repository", limits=Limits(tool_calls_per_run=7))
    _build(wanted, monkeypatch)

    assert seen, "build_agent never assembled a chain"
    assert seen[-1] is wanted, seen[-1]


def test_the_profile_prompt_is_counted_in_the_cacheable_prefix(monkeypatch):
    """The sidebar's cache warning is computed from this number.

    A profile that adds a long prompt can push a prefix over the model's
    cacheable floor; one that is dropped leaves the sidebar reporting a size
    the request does not have.
    """
    from mcp_agent.profiles import DEFAULT_PROFILE, Profile

    base = _build(DEFAULT_PROFILE, monkeypatch)
    with_prompt = _build(
        Profile(name="p", system_prompt="You are working in a git repository. " * 40),
        monkeypatch,
    )

    assert with_prompt.estimated_prefix_tokens > base.estimated_prefix_tokens


# --- Todos and context editing ---------------------------------------------------


def test_todos_are_off_unless_the_profile_asks():
    """They add a tool definition and about a page of system prompt.

    That is a real cost on a profile whose work is two commands long, and it
    lands in the cached prefix, so it is opt-in rather than a default.
    """
    from mcp_agent.profiles import DEFAULT_PROFILE, Profile

    assert "TodoListMiddleware" not in _names(DEFAULT_PROFILE)
    assert "TodoListMiddleware" not in _names(Profile(name="p"))


def test_a_profile_can_ask_for_todos():
    from mcp_agent.profiles import Profile

    assert "TodoListMiddleware" in _names(Profile(name="p", todos=True))


def test_todos_come_first_so_the_model_plans_before_anything_polices_it():
    from mcp_agent.profiles import Limits, Profile

    names = _names(
        Profile(name="p", todos=True, limits=Limits(tool_calls_per_run=5, model_calls_per_run=5))
    )
    assert names[0] == "TodoListMiddleware", names


def test_context_editing_is_off_unless_the_profile_asks():
    from mcp_agent.profiles import DEFAULT_PROFILE

    assert "ContextEditingMiddleware" not in _names(DEFAULT_PROFILE)


def test_a_profile_can_ask_for_old_tool_output_to_be_dropped():
    from mcp_agent.profiles import Profile

    assert "ContextEditingMiddleware" in _names(Profile(name="p", clear_tool_output_at=50_000))


def test_context_editing_runs_before_summarization():
    """Cheaper reclamation first.

    Dropping old tool output costs nothing but the output; summarizing spends
    a model call. Running them the other way round pays for the expensive one
    to reclaim what the cheap one would have.
    """
    from mcp_agent.profiles import Profile

    names = _names(Profile(name="p", clear_tool_output_at=50_000))
    assert names.index("ContextEditingMiddleware") < names.index("SummarizationMiddleware")


def test_the_profile_token_trigger_reaches_the_edit():
    from mcp_agent.agent import build_middleware
    from mcp_agent.profiles import Profile

    chain = build_middleware(Profile(name="p", clear_tool_output_at=1234), _model(), _spec())
    editing = [m for m in chain if type(m).__name__ == "ContextEditingMiddleware"][0]

    assert [edit.trigger for edit in editing.edits] == [1234]


def test_the_most_recent_tool_results_are_kept():
    """They are what the model is reasoning about now.

    Clearing them would make it repeat the calls it just made, which costs
    more than the tokens the edit reclaimed.
    """
    from mcp_agent.agent import build_middleware
    from mcp_agent.profiles import Profile

    chain = build_middleware(Profile(name="p", clear_tool_output_at=1000), _model(), _spec())
    edit = [m for m in chain if type(m).__name__ == "ContextEditingMiddleware"][0].edits[0]

    assert edit.keep >= 1
    assert edit.clear_tool_inputs is False, (
        "clearing the originating call would strand the cleared result"
    )


def test_the_whole_chain_in_order():
    """Everything a profile can ask for, at once, in the order it goes in.

    Asserted as one list rather than as pairwise comparisons: a reorder during
    a refactor moves one entry, and the pairwise version only notices if the
    pair it happened to check is the pair that moved.
    """
    from mcp_agent.profiles import Limits, Profile

    everything = Profile(
        name="p",
        todos=True,
        clear_tool_output_at=50_000,
        limits=Limits(tool_calls_per_run=10, model_calls_per_run=5),
    )

    assert _names(everything) == [
        "TodoListMiddleware",
        "ModelCallLimitMiddleware",
        "ToolCallLimitMiddleware",
        "ContextEditingMiddleware",
        "SummarizationMiddleware",
        "AnthropicPromptCachingMiddleware",
    ]


def test_the_shell_lands_in_the_chain_where_the_docstring_says(monkeypatch):
    """The chain-order test above has no shell, so it never covered this.

    Measured: deleting `chain.extend(build_shell_middleware(profile))`, and
    separately moving it to after prompt caching, both left the suite green —
    so the comment about capping a runaway before it reaches a command line was
    a comment and nothing else.
    """
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")

    from mcp_agent.profiles import Limits, Profile, ShellSettings

    profile = Profile(
        name="p",
        shell=ShellSettings(enabled=True, allow=("git",), approve=("git push",)),
        limits=Limits(tool_calls_per_run=10, model_calls_per_run=5),
        clear_tool_output_at=50_000,
    )

    assert _names(profile) == [
        "ModelCallLimitMiddleware",
        "ToolCallLimitMiddleware",
        "ShellAllowlistMiddleware",
        "HumanInTheLoopMiddleware",
        "RedactionArtifactScrubber",
        "ShellToolMiddleware",
        "ContextEditingMiddleware",
        "SummarizationMiddleware",
        "AnthropicPromptCachingMiddleware",
    ]


def test_no_shell_reaches_the_chain_when_the_operator_has_not_enabled_one(monkeypatch):
    monkeypatch.delenv("MCP_ENABLE_SHELL", raising=False)

    from mcp_agent.profiles import Profile, ShellSettings

    profile = Profile(name="p", shell=ShellSettings(enabled=True, allow=("git",)))

    assert not [name for name in _names(profile) if "Shell" in name]
