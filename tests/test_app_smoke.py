"""End-to-end script runs via Streamlit's own test harness.

These catch the class of breakage that unit tests miss: import errors, widget
misuse, and exceptions raised during a real script run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

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
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")


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
