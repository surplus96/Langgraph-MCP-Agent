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
from mcp_agent.agent import QueryResult, build_agent, run_query  # noqa: E402
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
    MODEL_REGISTRY,
    available_models,
)
from mcp_agent.runtime import run_sync  # noqa: E402
from mcp_agent.state import AppState  # noqa: E402

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
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


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
            agent, tool_count = run_sync(
                build_agent(state.selected_model, mcp_config, get_checkpointer()),
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

    state.agent = agent
    state.tool_count = tool_count
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

    if previous_model != state.selected_model and state.session_initialized:
        st.warning("⚠️ Model changed. Click 'Apply Settings' to re-initialize.")

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
                st.rerun()

    st.divider()


# --- Sidebar: system information and actions ----------------------------------

with st.sidebar:
    st.subheader("📊 System Information")
    st.write(f"🛠️ MCP Tools Count: {state.tool_count}")
    st.write(f"🧠 Current Model: {state.selected_model}")
    st.write(f"📐 Max Output Tokens: {MODEL_REGISTRY[state.selected_model].max_tokens:,}")

    if st.button("Apply Settings", type="primary", use_container_width=True):
        try:
            save_config(state.pending_mcp_config)
        except ConfigError as exc:
            logger.error("Could not save MCP config: %s", exc)
            st.error(f"❌ {exc}")
        else:
            if initialize_session(state.pending_mcp_config):
                st.success("✅ New settings have been applied.")
                st.rerun()

    st.divider()
    st.subheader("🔄 Actions")

    if st.button("Reset Conversation", use_container_width=True, type="primary"):
        state.reset_conversation()
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

            try:
                result = run_sync(
                    run_query(
                        state.agent,
                        user_query,
                        renderer,
                        thread_id=state.thread_id,
                        recursion_limit=state.recursion_limit,
                    ),
                    timeout=state.timeout_seconds,
                )
            except TimeoutError:
                logger.warning("Query exceeded %ss", state.timeout_seconds)
                result = QueryResult(error=f"⏱️ Request exceeded {state.timeout_seconds} seconds.")

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
