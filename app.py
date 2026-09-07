"""Streamlit front end for the LangGraph MCP agent.

This module is presentation only: model selection, agent construction, config
validation and streaming all live in ``src/mcp_agent``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent / "src"))

from mcp_agent import auth  # noqa: E402
from mcp_agent.agent import build_agent, discover_tools, run_query  # noqa: E402
from mcp_agent.config import (  # noqa: E402
    ConfigError,
    allowed_commands,
    config_path,
    load_config,
    save_config,
    validate_config,
    validate_server_config,
)
from mcp_agent.models import (  # noqa: E402
    DEFAULT_MODEL,
    EFFORT_LEVELS,
    MODEL_REGISTRY,
    available_models,
)
from mcp_agent.runtime import run_sync  # noqa: E402
from mcp_agent.state import AppState  # noqa: E402
from mcp_agent.usage import TokenUsage  # noqa: E402

# `override=False` so a real environment variable (from Compose, Kubernetes or a
# secret manager) wins over a file that happens to be mounted into the image.
load_dotenv(override=False)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

#: Registering an MCP server spawns a subprocess, so the in-app editor is an
#: administrative capability. It is off unless the operator opts in.
TOOL_EDITING_ENABLED = os.environ.get("MCP_ALLOW_TOOL_EDIT", "false").strip().lower() == "true"

state: AppState = AppState.get()


@st.cache_resource
def get_checkpointer():
    """One checkpointer for the process.

    Building it inside session initialization meant every "Apply Settings" click
    silently discarded conversation state while the UI still showed the history.

    Opened on the background loop because the SQLite connection belongs to
    whichever loop created it.
    """
    from mcp_agent.checkpoints import open_checkpointer

    return run_sync(open_checkpointer(), timeout=30)


def restore_thread() -> None:
    """Pin this browser session to a conversation that outlives the process.

    The thread id lives in the URL. That is what makes the durable checkpointer
    worth having: a checkpoint keyed by a UUID generated fresh on every session
    would be unreachable after a restart, so the data would be written and never
    read. In the URL it survives a reload, can be bookmarked, and gives each
    browser tab its own conversation for free.

    The transcript is reloaded alongside it. The checkpointer restores what the
    model remembers; the messages the user sees are Streamlit session state and
    are gone. Restoring one without the other comes back to a blank page and an
    agent that silently recalls everything.
    """
    stored = st.query_params.get("thread")
    if not stored:
        st.query_params["thread"] = state.thread_id
        return

    if stored == state.thread_id:
        return

    state.thread_id = stored
    if not state.history:
        from mcp_agent.checkpoints import load_history

        try:
            state.history = run_sync(load_history(get_checkpointer(), stored), timeout=30)
        except Exception:
            logger.exception("Could not restore the transcript for thread %s", stored)


# --- Login gate ---------------------------------------------------------------

login_required = auth.login_enabled()

st.set_page_config(
    page_title="Agent with MCP Tools",
    page_icon="🧠",
    layout="centered" if (login_required and not state.authenticated) else "wide",
)

if login_required and not state.authenticated:
    st.title("🔐 Login")

    # Refuse to present a login form we cannot check credentials against.
    # Compose turns an unset USER_ID into "", which previously made a blank
    # submission authenticate successfully.
    try:
        auth.expected_credentials()
    except auth.AuthConfigError as exc:
        logger.error("Login gate misconfigured: %s", exc)
        st.error(f"⚠️ {exc}")
        st.stop()

    st.markdown("Login is required to use the system.")

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            try:
                if auth.verify(username, password):
                    state.authenticated = True
                    st.rerun()
                else:
                    st.error("❌ Username or password is incorrect.")
            except auth.AuthConfigError as exc:
                logger.error("Login gate misconfigured: %s", exc)
                st.error(f"⚠️ {exc}")
    st.stop()


# --- Conversation identity ----------------------------------------------------
#
# After the login gate, deliberately: restoring a stored transcript is reading
# conversation content, which an unauthenticated visitor must not be able to
# trigger by putting a thread id in the URL.

restore_thread()


# --- Header -------------------------------------------------------------------

st.title("💬 MCP Tool Utilization Agent")
st.markdown("✨ Ask questions to the ReAct agent that utilizes MCP tools.")


# --- Rendering ----------------------------------------------------------------


class StreamlitRenderer:
    """Writes streamed output into two Streamlit placeholders."""

    def __init__(self, text_placeholder: Any, tool_placeholder: Any) -> None:
        self._text = text_placeholder
        self._tool = tool_placeholder

    def on_text(self, text: str) -> None:
        self._text.markdown(text)

    def on_tool(self, tool_log: str) -> None:
        with self._tool.expander("🔧 Tool Call Information", expanded=True):
            # st.code, not st.markdown: tool output is untrusted and a literal
            # fence inside it would otherwise escape into live markdown.
            st.code(tool_log, language="json")


def render_usage(usage: TokenUsage) -> None:
    """Show token spend, how much came from cache, and why if none did.

    A 0% hit rate has four plausible causes, and blaming only prefix instability
    sends people to debug a problem they may not have. The most common cause for
    this app is the least obvious: a prefix shorter than the model's minimum
    cacheable length is ignored silently, with no error anywhere.
    """
    spec = MODEL_REGISTRY[state.selected_model]
    st.divider()
    st.subheader("🧮 Token Usage")

    prefix_too_short = state.prefix_tokens and state.prefix_tokens < spec.min_cacheable_tokens
    if prefix_too_short:
        st.warning(
            f"Caching is inactive: the prompt prefix is roughly "
            f"{state.prefix_tokens:,} tokens, under this model's "
            f"{spec.min_cacheable_tokens:,}-token minimum. Anthropic ignores "
            "cache_control below that, without an error. Add more MCP tools, or "
            "pick a model with a lower floor."
        )

    if not usage.input_tokens and not usage.output_tokens:
        st.caption("No requests yet this session.")
        return

    col1, col2 = st.columns(2)
    col1.metric("Input (billed)", f"{usage.uncached_input:,}")
    col2.metric("Output", f"{usage.output_tokens:,}")

    st.metric("Cache hit rate", f"{usage.cache_hit_rate:.1%}")
    st.caption(
        f"cache read {usage.cache_read:,} · "
        f"cache write {usage.cache_creation:,} · "
        f"total input {usage.input_tokens:,}"
    )

    if usage.cache_hit_rate == 0 and not prefix_too_short:
        st.caption(
            "0% is expected on the first turn — nothing is cached yet. If it "
            "persists, the prefix is changing between turns (a tool set edit "
            "does that) or the cache expired between turns."
        )


def render_server_failures() -> None:
    """Name the MCP servers that failed, if any.

    The pool skips a broken server rather than failing the whole run, which is
    the right behaviour and also an invisible one: without this, the only
    symptom is a tool count quietly lower than expected, and the reason is in a
    log the user is not reading.
    """
    from mcp_agent.sessions import current_pool

    pool = current_pool()
    if pool is None or pool.healthy:
        return

    for failure in pool.failures:
        when = "failed to start" if failure.at_startup else "stopped responding"
        st.warning(f"⚠️ MCP server **{failure.server_name}** {when}: {failure.error}")


def render_history() -> None:
    """Replay the conversation."""
    for message in state.history:
        avatar = "🧑‍💻" if message["role"] == "user" else "🤖"
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])
            tool_log = message.get("tool_log")
            if tool_log:
                with st.expander("🔧 Tool Call Information", expanded=False):
                    st.code(tool_log, language="json")


# --- Session initialization ---------------------------------------------------


def initialize_session(mcp_config: dict[str, Any]) -> bool:
    """Connect to MCP servers and build the agent. Returns success."""
    try:
        with st.spinner("🔄 Connecting to MCP server..."):
            tools = run_sync(discover_tools(mcp_config), timeout=120)
            bundle = run_sync(
                build_agent(
                    state.selected_model,
                    tools,
                    get_checkpointer(),
                    effort=state.selected_effort,
                ),
                timeout=state.timeout_seconds,
            )
    except TimeoutError:
        logger.error("MCP connection timed out")
        st.error("⏱️ Timed out connecting to the MCP server(s).")
        return False
    except Exception as exc:
        logger.exception("Session initialization failed")
        st.error(f"❌ Could not initialize the agent: {exc}")
        return False

    state.agent = bundle.agent
    state.tool_count = bundle.tool_count
    state.prefix_tokens = bundle.estimated_prefix_tokens
    state.session_initialized = True
    return True


# --- Sidebar: system settings -------------------------------------------------

with st.sidebar:
    st.subheader("⚙️ System Settings")

    models = available_models()
    if not models:
        st.warning("⚠️ No API key configured. Add ANTHROPIC_API_KEY to your .env file.")
        models = [DEFAULT_MODEL]

    if state.selected_model not in models:
        state.selected_model = models[0]

    previous_model = state.selected_model
    state.selected_model = st.selectbox(
        "🤖 Select model to use",
        options=models,
        index=models.index(state.selected_model),
        help="Anthropic models require ANTHROPIC_API_KEY to be set.",
    )

    spec = MODEL_REGISTRY[state.selected_model]
    previous_effort = state.selected_effort

    if spec.supports_effort:
        state.selected_effort = st.select_slider(
            "🎚️ Effort",
            options=EFFORT_LEVELS,
            value=state.selected_effort,
            help=(
                "How hard the model works before answering. Lower means fewer, "
                "more consolidated tool calls and less preamble — good for "
                "routine lookups. Raise it for multi-step work. Changing this "
                "invalidates the cached prompt prefix for the next turn."
            ),
        )
    else:
        st.caption(f"🎚️ Effort is not supported on {state.selected_model}.")

    if state.session_initialized and (
        previous_model != state.selected_model or previous_effort != state.selected_effort
    ):
        st.warning("⚠️ Setting changed. Click 'Apply Settings' to re-initialize.")

    state.timeout_seconds = st.slider(
        "⏱️ Response generation time limit (seconds)",
        min_value=60,
        max_value=600,
        value=state.timeout_seconds,
        step=30,
        help="How long to wait for the agent to finish a turn.",
    )

    state.recursion_limit = st.slider(
        "⏳ Recursion limit",
        min_value=10,
        max_value=200,
        value=state.recursion_limit,
        step=10,
        help="Maximum graph steps per turn. Each step can cost a model call.",
    )

    st.divider()
    st.subheader("🔧 Tool Settings")

    try:
        if state.pending_mcp_config is None:
            state.pending_mcp_config = validate_config(load_config())
    except ConfigError as exc:
        logger.error("Could not load MCP config: %s", exc)
        st.error(f"❌ {exc}")
        st.info(f"Fix or remove {config_path()} and reload. Existing settings are left untouched.")
        state.pending_mcp_config = state.pending_mcp_config or {}

    if TOOL_EDITING_ENABLED:
        with st.expander("🧰 Add MCP Tools", expanded=False):
            st.markdown(
                "Insert **one tool** in JSON format, wrapped in curly braces.\n\n"
                f"Allowed commands: `{'`, `'.join(allowed_commands())}`"
            )
            example = {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github@2026.4.1"],
                    "transport": "stdio",
                }
            }
            new_tool_json = st.text_area(
                "Tool JSON",
                json.dumps(example, indent=2, ensure_ascii=False),
                height=220,
            )

            if st.button("Add Tool", type="primary", use_container_width=True):
                try:
                    parsed = json.loads(new_tool_json)
                    if "mcpServers" in parsed:
                        parsed = parsed["mcpServers"]
                        st.info("'mcpServers' format detected. Converting.")
                    if not isinstance(parsed, dict) or not parsed:
                        raise ConfigError("Please enter at least one tool.")

                    added = []
                    for name, entry in parsed.items():
                        state.pending_mcp_config[name] = validate_server_config(name, entry)
                        added.append(name)

                    state.config_dirty = True
                    st.success(f"Added: {', '.join(added)}. Click 'Apply Settings' to apply.")
                    st.rerun()
                except json.JSONDecodeError as exc:
                    st.error(f"JSON parsing error: {exc}")
                except ConfigError as exc:
                    st.error(f"❌ {exc}")
    else:
        st.info(
            "🔒 In-app tool editing is disabled. Registering an MCP server starts a "
            f"subprocess, so edit {config_path()} directly, or set "
            "`MCP_ALLOW_TOOL_EDIT=true` to enable the editor."
        )

    with st.expander("📋 Registered Tools List", expanded=True):
        if not state.pending_mcp_config:
            st.caption("No MCP servers configured.")
        for tool_name in list(state.pending_mcp_config):
            col1, col2 = st.columns([8, 2])
            col1.markdown(f"- **{tool_name}**")
            if TOOL_EDITING_ENABLED and col2.button("Delete", key=f"del_{tool_name}"):
                del state.pending_mcp_config[tool_name]
                state.config_dirty = True
                st.rerun()

    st.divider()


# --- Sidebar: system information and actions ----------------------------------

with st.sidebar:
    st.subheader("📊 System Information")
    st.write(f"🛠️ MCP Tools Count: {state.tool_count}")
    render_server_failures()
    st.write(f"🧠 Current Model: {state.selected_model}")
    spec = MODEL_REGISTRY[state.selected_model]
    st.write(f"📐 Max Output Tokens: {spec.max_tokens:,}")
    if spec.supports_effort:
        st.write(f"🎚️ Effort: {state.selected_effort}")
    st.write(f"📥 Context Window: {spec.context_window:,}")

    render_usage(state.usage)

    if st.button("Apply Settings", type="primary", use_container_width=True):
        try:
            if state.config_dirty:
                save_config(state.pending_mcp_config)
                state.config_dirty = False
            else:
                # Nothing was edited in this session, so the file is
                # authoritative — re-read it. Writing the session's copy back
                # unconditionally silently discarded an edit made on disk,
                # which is the only way to add a tool when the editor is off.
                state.pending_mcp_config = validate_config(load_config())
        except ConfigError as exc:
            logger.error("Could not apply MCP config: %s", exc)
            st.error(f"❌ {exc}")
        else:
            if initialize_session(state.pending_mcp_config):
                st.success("✅ New settings have been applied.")
                st.rerun()

    st.divider()
    st.subheader("🔄 Actions")

    if st.button("Reset Conversation", use_container_width=True, type="primary"):
        state.reset_conversation()
        # The id in the URL has to move too. Leaving it would send the next
        # reload straight back into the conversation just abandoned.
        st.query_params["thread"] = state.thread_id
        st.rerun()

    if login_required and state.authenticated:
        st.divider()
        if st.button("Logout", use_container_width=True, type="secondary"):
            state.authenticated = False
            st.rerun()


# --- Main pane ----------------------------------------------------------------

if not state.session_initialized:
    st.info(
        "MCP server and agent are not initialized. "
        "Click 'Apply Settings' in the sidebar to initialize."
    )

render_history()

user_query = st.chat_input("💬 Enter your question")
if user_query:
    if not state.session_initialized:
        st.warning("⚠️ Agent is not initialized. Click 'Apply Settings' in the sidebar.")
    else:
        st.chat_message("user", avatar="🧑‍💻").markdown(user_query)

        with st.chat_message("assistant", avatar="🤖"):
            tool_placeholder = st.empty()
            text_placeholder = st.empty()
            renderer = StreamlitRenderer(text_placeholder, tool_placeholder)

            # run_query applies the timeout itself and returns the text and
            # tokens it managed to produce. Cancelling from out here instead
            # would strand both inside its frame — which is what this code
            # used to do, discarding exactly the longest, most expensive turns.
            result = run_sync(
                run_query(
                    state.agent,
                    user_query,
                    renderer,
                    thread_id=state.thread_id,
                    recursion_limit=state.recursion_limit,
                    timeout_seconds=state.timeout_seconds,
                ),
                timeout=state.timeout_seconds + 30,
            )

        state.usage = state.usage + result.usage
        state.history.append({"role": "user", "content": user_query})
        if result.error:
            st.error(result.error)
            state.history.append(
                {
                    "role": "assistant",
                    "content": result.text or result.error,
                    "tool_log": result.tool_log,
                }
            )
        else:
            state.history.append(
                {
                    "role": "assistant",
                    "content": result.text,
                    "tool_log": result.tool_log,
                }
            )
        st.rerun()
