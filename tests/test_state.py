"""Session state defaults and conversation reset."""

from __future__ import annotations

from mcp_agent.models import DEFAULT_MODEL
from mcp_agent.state import AppState


def test_defaults_are_safe():
    state = AppState()
    assert state.authenticated is False
    assert state.session_initialized is False
    assert state.selected_model == DEFAULT_MODEL
    assert state.history == []
    assert state.tool_count == 0


def test_each_instance_gets_its_own_history():
    a, b = AppState(), AppState()
    a.history.append({"role": "user", "content": "x"})
    assert b.history == []


def test_each_instance_gets_its_own_thread_id():
    assert AppState().thread_id != AppState().thread_id


def test_reset_clears_history_and_rotates_thread():
    state = AppState()
    original_thread = state.thread_id
    state.history.append({"role": "user", "content": "x"})

    state.reset_conversation()

    assert state.history == []
    assert state.thread_id != original_thread


def test_reset_clears_accumulated_usage():
    """Usage is per-conversation; a reset must not carry the old totals over."""
    from mcp_agent.usage import TokenUsage

    state = AppState(usage=TokenUsage(input_tokens=500, cache_read=400))
    state.reset_conversation()
    assert state.usage == TokenUsage()
