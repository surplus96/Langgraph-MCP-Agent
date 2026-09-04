"""A single long-lived event loop, shared by the whole process.

Streamlit runs each script rerun on a synchronous thread and still has no native
asyncio support, so async work has to be marshalled somewhere. The previous
approach — ``nest_asyncio.apply()`` plus a fresh loop per session stashed in
``st.session_state`` — had three problems: the loops were never closed, the
per-session lifetime did not match ``set_event_loop``'s per-thread scope, and
re-entrant loop patching corrupts the anyio cancel scopes that the MCP stdio
transport relies on.

One daemon-thread loop for the process avoids all three.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide background loop, starting it on first use."""
    global _loop

    with _lock:
        if _loop is not None and not _loop.is_closed():
            return _loop

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="mcp-agent-loop", daemon=True)
        thread.start()
        _loop = loop
        return loop


def run_sync[T](coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
    """Run ``coro`` on the background loop and block until it finishes.

    Args:
        coro: The coroutine to run.
        timeout: Seconds to wait before cancelling.

    Raises:
        TimeoutError: if ``timeout`` elapses first. The coroutine is cancelled.
    """
    future = asyncio.run_coroutine_threadsafe(coro, get_loop())
    try:
        return future.result(timeout)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"Operation exceeded {timeout} seconds.") from exc
