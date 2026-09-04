"""Streaming a LangGraph run into a callback.

Reduced from the original ``utils.py``, which carried a second near-identical
``ainvoke_graph`` with no callers, an ``updates`` streaming branch this
application never selected, and ANSI-coloured ``print`` fallbacks that could not
be reached because a callback is always supplied.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph


async def astream_graph(
    graph: CompiledStateGraph,
    inputs: dict[str, Any],
    callback: Callable[[dict[str, Any]], Any],
    config: RunnableConfig | None = None,
    node_names: list[str] | None = None,
) -> dict[str, Any]:
    """Stream ``graph`` message-by-message, handing each chunk to ``callback``.

    Args:
        graph: The compiled graph to run.
        inputs: Initial graph state.
        callback: Invoked with ``{"node": str, "content": Any}`` per chunk. May
            be sync or async.
        config: Runnable configuration.
        node_names: If given, only chunks from these nodes reach the callback.

    Returns:
        The final chunk seen, or ``{}`` if the graph produced none.
    """
    config = config or RunnableConfig()
    node_names = node_names or []
    final_result: dict[str, Any] = {}

    async for chunk_msg, metadata in graph.astream(inputs, config, stream_mode="messages"):
        meta: dict[str, Any] = metadata if isinstance(metadata, dict) else {}
        current_node = meta.get("langgraph_node", "unknown")
        final_result = {
            "node": current_node,
            "content": chunk_msg,
            "metadata": meta,
        }

        if node_names and current_node not in node_names:
            continue

        result = callback({"node": current_node, "content": chunk_msg})
        if inspect.isawaitable(result):
            await result

    return final_result
