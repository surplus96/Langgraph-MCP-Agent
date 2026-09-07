"""Prompt caching: token accounting, and that cache_control is actually applied.

The middleware's effect cannot be seen from the agent's return value — it
changes the outbound request. These tests intercept that request through the
middleware's own public hook, so "caching is wired up" is a checked claim
rather than an assumption. Whether the cache is *hit* still needs a live call;
the app reports that as a hit rate in the sidebar.
"""

from __future__ import annotations

import os

import pytest
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from mcp_agent.agent import TokenUsage


@tool
def alpha(value: str) -> str:
    """First tool."""
    return value


@tool
def beta(value: str) -> str:
    """Second tool."""
    return value


def build_request():
    from langchain.agents.middleware.types import ModelRequest
    from langchain_anthropic import ChatAnthropic

    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

    return ModelRequest(
        model=ChatAnthropic(model="claude-opus-5", max_tokens=1024),  # type: ignore[call-arg]
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="You are a helpful agent."),
        tool_choice=None,
        tools=[alpha, beta],
        response_format=None,
        state={"messages": []},
        runtime=None,
        model_settings={},
    )


def apply_middleware():
    """Run the middleware and return the request it produced.

    A fresh closure per call: a module-level spy would let a later test read a
    request captured by an earlier one and pass without the middleware running.
    """
    box: dict = {}

    def capture(request):
        box["request"] = request
        return None

    AnthropicPromptCachingMiddleware().wrap_model_call(build_request(), capture)
    assert "request" in box, "middleware did not invoke the handler"
    return box["request"]


def test_cache_control_reaches_model_settings():
    """This is a top-level breakpoint that follows the growing message tail.

    It is the third breakpoint, beyond the system prompt and the tool block,
    and the one that makes multi-step ReAct turns cheap.
    """
    assert apply_middleware().model_settings.get("cache_control") == {
        "type": "ephemeral",
        "ttl": "5m",
    }


def test_system_prompt_is_tagged():
    system = apply_middleware().system_message
    assert "cache_control" in str(system.content)


def test_last_tool_definition_is_tagged():
    """One trailing breakpoint caches the whole contiguous tool block."""
    assert "cache_control" in str(apply_middleware().tools[-1])


# --- Token accounting ---------------------------------------------------------


def test_usage_starts_empty():
    usage = TokenUsage()
    assert usage.input_tokens == 0
    assert usage.cache_hit_rate == 0.0


def test_hit_rate_of_a_cold_turn_is_zero():
    """Nothing is cached on the first turn, so 0% is correct, not a failure."""
    usage = TokenUsage(input_tokens=1000, output_tokens=50, cache_creation=900)
    assert usage.cache_hit_rate == 0.0
    assert usage.uncached_input == 100


def test_hit_rate_of_a_warm_turn():
    usage = TokenUsage(input_tokens=1000, output_tokens=50, cache_read=900)
    assert usage.cache_hit_rate == 0.9
    assert usage.uncached_input == 100


def test_usage_adds_across_turns():
    """Every field must sum from both sides.

    An earlier version of this test gave each cache field its only nonzero
    value on one side, so "sum" and "take one side" were indistinguishable —
    and dropping a term from __add__ left the suite green. This is the path
    the sidebar's session total runs through on every turn.
    """
    first = TokenUsage(input_tokens=100, output_tokens=10, cache_read=30, cache_creation=80)
    second = TokenUsage(input_tokens=120, output_tokens=12, cache_read=70, cache_creation=5)
    total = first + second
    assert total == TokenUsage(
        input_tokens=220, output_tokens=22, cache_read=100, cache_creation=85
    )


def test_usage_accumulates_over_many_turns():
    """Repeated accumulation, as a session actually does it."""
    running = TokenUsage()
    for _ in range(4):
        running = running + TokenUsage(
            input_tokens=100, output_tokens=10, cache_read=60, cache_creation=20
        )
    assert running == TokenUsage(
        input_tokens=400, output_tokens=40, cache_read=240, cache_creation=80
    )


def test_uncached_input_never_goes_negative():
    """Defensive: a provider reporting overlapping counts must not print a minus."""
    usage = TokenUsage(input_tokens=10, cache_read=8, cache_creation=8)
    assert usage.uncached_input == 0


def test_from_metadata_reads_nested_details():
    usage = TokenUsage.from_metadata(
        {
            "input_tokens": 500,
            "output_tokens": 20,
            "total_tokens": 520,
            "input_token_details": {"cache_read": 400, "cache_creation": 0},
        }
    )
    assert usage.input_tokens == 500
    assert usage.cache_read == 400


def test_per_ttl_cache_writes_are_counted():
    """langchain-anthropic zeroes the generic key and reports a TTL split.

    Verified against the installed library: when Anthropic returns the
    breakdown, `input_token_details["cache_creation"]` is set to 0 and the
    counts move to the ephemeral_* keys. Reading only the generic key loses
    the entire cache-write figure.
    """
    usage = TokenUsage.from_metadata(
        {
            "input_tokens": 1000,
            "output_tokens": 5,
            "total_tokens": 1005,
            "input_token_details": {
                "cache_read": 0,
                "cache_creation": 0,
                "ephemeral_5m_input_tokens": 600,
                "ephemeral_1h_input_tokens": 300,
            },
        }
    )
    assert usage.cache_creation == 900
    assert usage.uncached_input == 100


def test_generic_cache_write_key_wins_when_present():
    """The two sources must not be added together."""
    usage = TokenUsage.from_metadata(
        {
            "input_tokens": 1000,
            "input_token_details": {
                "cache_creation": 500,
                "ephemeral_5m_input_tokens": 500,
            },
        }
    )
    assert usage.cache_creation == 500


def test_none_valued_detail_fields_are_treated_as_zero():
    """A real shape: the library builds these with getattr(..., None)."""
    usage = TokenUsage.from_metadata(
        {
            "input_tokens": 10,
            "input_token_details": {"cache_read": None, "cache_creation": None},
        }
    )
    assert usage == TokenUsage(input_tokens=10)


@pytest.mark.parametrize(
    "value,expected",
    [
        (1200, 1200),
        ("1200", 1200),
        (1200.7, 1200),
        (None, 0),
        (True, 0),  # bool is an int subclass but never a token count
        ("abc", 0),
        ({"nested": 1}, 0),
    ],
)
def test_usage_fields_are_coerced_rather_than_raising(value, expected):
    """Accounting must never be able to fail a turn."""
    from mcp_agent.usage import _as_int

    assert _as_int("input_tokens", value) == expected


def test_from_metadata_tolerates_missing_pieces():
    assert TokenUsage.from_metadata(None) == TokenUsage()
    assert TokenUsage.from_metadata({"input_tokens": 5}) == TokenUsage(input_tokens=5)


@pytest.mark.parametrize("hit_rate,expected", [(0, 0.0), (500, 0.5), (1000, 1.0)])
def test_hit_rate_scales(hit_rate: int, expected: float):
    assert TokenUsage(input_tokens=1000, cache_read=hit_rate).cache_hit_rate == expected


# --- The wiring, not the library ----------------------------------------------
#
# The three tests above characterise langchain-anthropic. They pass whether or
# not this repository uses it: deleting the middleware from build_agent leaves
# them green. These pin our own call.


def test_build_agent_attaches_the_caching_middleware(monkeypatch):
    """Removing the middleware from build_agent must fail the suite."""
    import asyncio

    from mcp_agent import agent as agent_module

    captured: dict = {}

    def fake_create_agent(model, tools, **kwargs):
        captured.update(kwargs)
        return "compiled-agent"

    monkeypatch.setattr("langchain.agents.create_agent", fake_create_agent)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    bundle = asyncio.run(agent_module.build_agent("claude-opus-5", [alpha, beta], None))

    middleware = captured.get("middleware") or []
    assert any(isinstance(m, AnthropicPromptCachingMiddleware) for m in middleware), (
        "build_agent no longer attaches the prompt caching middleware"
    )
    assert bundle.tool_count == 2
    assert bundle.estimated_prefix_tokens > 0


def test_discover_tools_sorts_for_a_stable_cache_prefix(monkeypatch):
    """Tool order is part of the cached prefix; an unstable order misses.

    Sorting moved to discover_tools when discovery was split out so the caller
    could cache it — the ~580 ms cost does not warm up on its own.
    """
    import asyncio

    from mcp_agent.agent import discover_tools

    class FakeClient:
        def __init__(self, config):
            pass

        async def get_tools(self):
            return [beta, alpha]  # deliberately unsorted

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    tools = asyncio.run(discover_tools({}))
    assert [t.name for t in tools] == ["alpha", "beta"]


def test_ttl_falls_back_on_a_bad_value(monkeypatch):
    from mcp_agent.agent import _prompt_cache_ttl

    monkeypatch.setenv("PROMPT_CACHE_TTL", "30m")
    assert _prompt_cache_ttl() == "1h"
    monkeypatch.setenv("PROMPT_CACHE_TTL", "5m")
    assert _prompt_cache_ttl() == "5m"


def test_summarization_and_caching_are_both_attached(monkeypatch):
    """Adding history summarization must not displace the caching middleware."""
    import asyncio

    from langchain.agents.middleware import SummarizationMiddleware

    from mcp_agent import agent as agent_module

    captured: dict = {}

    def fake_create_agent(model, tools, **kwargs):
        captured.update(kwargs)
        return "compiled-agent"

    monkeypatch.setattr("langchain.agents.create_agent", fake_create_agent)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    asyncio.run(agent_module.build_agent("claude-opus-5", [alpha], None))

    kinds = {type(m) for m in captured.get("middleware") or []}
    assert AnthropicPromptCachingMiddleware in kinds
    assert SummarizationMiddleware in kinds


def test_summarization_triggers_late_enough_to_protect_the_cache(monkeypatch):
    """Summarizing rewrites history and drops the cached prefix.

    It has to be rare, so the trigger must sit well into the context window
    rather than near the start of it.
    """
    from mcp_agent.agent import SUMMARIZE_AT_FRACTION

    assert 0.5 < SUMMARIZE_AT_FRACTION < 1.0
