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


# --- Recording a turn that stopped for a person --------------------------------


def _result(**kwargs):
    from mcp_agent.agent import QueryResult

    return QueryResult(**kwargs)


def _pending(command: str = "git push"):
    from mcp_agent.approvals import PendingApproval

    return PendingApproval(tool_name="shell", command=command, args={"command": command})


def test_a_finished_turn_closes_an_assistant_bubble():
    state = AppState()

    state.record(_result(text="Half past four."), user_query="what time is it")

    assert state.history == [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "Half past four.", "tool_log": ""},
    ]
    assert state.pending_approval is None


def test_a_turn_waiting_on_a_person_does_not_close_one():
    """It is unfinished, not finished. The same turn continues after the click.

    Closing a bubble here would put an empty assistant message in the
    transcript and then a second one when the turn resumes — two answers shown
    for one question.
    """
    state = AppState()

    state.record(_result(pending_approval=_pending()), user_query="push it")

    assert state.history == [{"role": "user", "content": "push it"}]
    assert state.pending_approval == _pending()


def test_text_streamed_before_the_stop_is_not_thrown_away():
    state = AppState()

    state.record(_result(text="Pushing now.", pending_approval=_pending()), user_query="push")

    assert state.history[-1] == {
        "role": "assistant",
        "content": "Pushing now.",
        "tool_log": "",
    }
    assert state.pending_approval is not None


def test_resuming_clears_the_pending_approval():
    state = AppState()
    state.record(_result(pending_approval=_pending()), user_query="push it")

    state.record(_result(text="Done."))

    assert state.pending_approval is None
    assert state.history[-1]["content"] == "Done."


def test_a_resumed_turn_adds_no_second_user_message():
    """The question was asked once; the decision is not a new one."""
    state = AppState()
    state.record(_result(pending_approval=_pending()), user_query="push it")

    state.record(_result(text="Done."))

    assert [m for m in state.history if m["role"] == "user"] == [
        {"role": "user", "content": "push it"}
    ]


def test_an_error_still_reaches_the_transcript():
    state = AppState()

    state.record(_result(error="The agent produced no output."), user_query="hello")

    assert "no output" in state.history[-1]["content"]


def test_usage_accumulates_across_a_stop_and_a_resume():
    from mcp_agent.usage import TokenUsage

    state = AppState()
    state.record(
        _result(pending_approval=_pending(), usage=TokenUsage(input_tokens=10)),
        user_query="push",
    )
    state.record(_result(text="Done.", usage=TokenUsage(input_tokens=5)))

    assert state.usage.input_tokens == 15


def test_resetting_drops_a_command_waiting_for_approval():
    """It belonged to the thread being abandoned.

    Left set, the page would offer to approve a call into a conversation that
    no longer exists, and resuming would write a checkpoint under the new id
    with nothing there to resume.
    """
    state = AppState()
    state.record(_result(pending_approval=_pending()), user_query="push it")

    state.reset_conversation()

    assert state.pending_approval is None
