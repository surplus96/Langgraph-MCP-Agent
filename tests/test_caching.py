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


def capture(request):
    """Stand in for the model call, recording what the middleware produced."""
    capture.request = request
    return None


def apply_middleware():
    middleware = AnthropicPromptCachingMiddleware()
    middleware.wrap_model_call(build_request(), capture)
    return capture.request


def test_cache_control_reaches_model_settings():
    """The middleware sets a ttl alongside the type; 5m is its default."""
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
    first = TokenUsage(input_tokens=100, output_tokens=10, cache_creation=80)
    second = TokenUsage(input_tokens=120, output_tokens=12, cache_read=80)
    total = first + second
    assert total.input_tokens == 220
    assert total.output_tokens == 22
    assert total.cache_read == 80
    assert total.cache_creation == 80


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


def test_from_metadata_tolerates_missing_pieces():
    assert TokenUsage.from_metadata(None) == TokenUsage()
    assert TokenUsage.from_metadata({"input_tokens": 5}) == TokenUsage(input_tokens=5)


@pytest.mark.parametrize("hit_rate,expected", [(0, 0.0), (500, 0.5), (1000, 1.0)])
def test_hit_rate_scales(hit_rate: int, expected: float):
    assert TokenUsage(input_tokens=1000, cache_read=hit_rate).cache_hit_rate == expected
