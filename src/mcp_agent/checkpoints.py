"""Conversation state that survives a process restart.

An in-memory checkpointer loses every conversation when Streamlit restarts,
which for a local app is most days. This swaps it for SQLite on the same volume
that already holds ``config.json``.

Two things make the difference between a durable checkpointer that works and one
that only looks like it does:

- **The thread id has to survive too.** A checkpoint keyed by a UUID generated
  fresh on every browser session is unreachable after a restart — the data is
  there and nothing will ever ask for it. ``app.py`` keeps the id in the URL's
  query string for that reason.
- **The transcript has to be re-read.** The checkpointer restores what the
  *model* remembers; the messages the *user* sees live in Streamlit session
  state, which is gone. Without :func:`load_history` the page comes back blank
  while the agent silently recalls everything, which is worse than forgetting.

Falling back to memory is deliberate. A read-only filesystem or an unwritable
volume should cost you persistence, not the application.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Set to this to ask for the old behaviour explicitly rather than by accident.
DISABLED = ":memory:"


def checkpoint_path() -> str:
    """Where the checkpoint database lives.

    Defaults alongside ``config.json`` under ``data/`` so a container that
    already mounts that volume gets persistence with no extra configuration.
    """
    return os.environ.get("CHECKPOINT_DB_PATH", "data/checkpoints.db").strip()


async def open_checkpointer() -> Any:
    """Return a checkpointer, durable if the database can be opened.

    Must be awaited on the loop from :mod:`mcp_agent.runtime`: the underlying
    ``aiosqlite`` connection dispatches to a worker thread owned by the loop
    that created it, so using it from another one would not be safe.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    path = checkpoint_path()
    if not path or path == DISABLED:
        logger.info("CHECKPOINT_DB_PATH=%r; conversations will not survive a restart", path)
        return InMemorySaver()

    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

        connection = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
    except Exception as exc:
        # Losing persistence is a degradation; losing the app is not acceptable.
        # A read-only rootfs or an unwritable mount lands here.
        logger.warning(
            "Could not open the checkpoint database at %s (%s); "
            "falling back to in-memory conversation state",
            path,
            exc,
        )
        return InMemorySaver()

    logger.info("Conversation state persisted to %s", path)
    return saver


def _text_of(content: Any) -> str:
    """Flatten a message's content to what the transcript should show.

    Anthropic messages arrive as a list of blocks. Only the text blocks belong
    in the transcript; thinking and tool-use blocks are rendered separately, or
    not at all.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts)
    return str(content)


async def load_history(checkpointer: Any, thread_id: str) -> list[dict[str, Any]]:
    """Rebuild the displayed transcript for ``thread_id``.

    Returns the same shape ``app.py`` appends as a conversation runs, so the
    caller can drop it straight into session state. An empty list means there is
    nothing stored — a first visit, a cleared database, or a thread id that was
    never used. That is not an error.

    Tool calls are not reconstructed. They are in the checkpoint, but the
    per-turn log the UI shows is assembled from the stream rather than from
    message history, and inventing a different-looking one would be worse than
    showing none.
    """
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.runnables import RunnableConfig

    try:
        tuple_ = await checkpointer.aget_tuple(
            RunnableConfig(configurable={"thread_id": thread_id})
        )
    except Exception:
        logger.exception("Could not read stored history for thread %s", thread_id)
        return []

    if tuple_ is None:
        return []

    messages = (tuple_.checkpoint.get("channel_values") or {}).get("messages") or []

    history: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        else:
            # Tool results and system messages are context, not transcript.
            continue

        text = _text_of(message.content)
        if not text.strip():
            # An assistant turn that only made tool calls carries no text of its
            # own. Rendering it would put an empty bubble in the transcript.
            continue
        history.append({"role": role, "content": text})

    return history
