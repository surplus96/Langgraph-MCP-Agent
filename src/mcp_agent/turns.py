"""Running one agent turn across the thread boundary.

The agent runs on the background loop from :mod:`mcp_agent.runtime`. The user
interface runs on Streamlit's script thread. Streaming output has to cross that
boundary, and the obvious way to do it is wrong:

    result = run_sync(run_query(agent, query, renderer, ...))

That marshals the *whole* coroutine to the loop — renderer included — so every
``st.markdown`` call happens on a thread with no ``ScriptRunContext``, where
Streamlit raises ``NoSessionContext``. ``run_query`` catches it like any other
exception and returns it as an error string, so the symptom is not a traceback
but a turn that dies at its first chunk with a blank reason. Reproduced by
driving the real script: the renderer was called on ``mcp-agent-loop`` and
raised on the first write.

The tempting one-line fix is to attach a script-run context to the loop thread.
That is worse than the bug. The loop is process-wide and shared by every
browser session, so writes would land in whichever session happened to attach
last.

So the events cross the boundary as data. :class:`Turn` runs the coroutine on
the loop and hands events to whichever thread iterates it; the caller does all
its drawing there.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from dataclasses import dataclass
from typing import Any, Literal

from mcp_agent.agent import QueryResult, run_query
from mcp_agent.runtime import get_loop

logger = logging.getLogger(__name__)

#: How long to wait on the queue before checking whether the turn has finished.
#: Short enough that the last chunk is not left sitting after the agent
#: returns, long enough not to spin.
_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class TurnEvent:
    """One thing to draw: the accumulated answer, or the accumulated tool log."""

    kind: Literal["text", "tool"]
    payload: str


class _QueuedRenderer:
    """A ChunkRenderer that hands events to another thread instead of drawing.

    Deliberately does nothing that could fail. It is called from the background
    loop, inside the agent's stream, and an exception here would surface as a
    failed turn rather than a failed repaint.
    """

    def __init__(self, events: queue.Queue[TurnEvent]) -> None:
        self._events = events

    def on_text(self, text: str) -> None:
        self._events.put(TurnEvent("text", text))

    def on_tool(self, tool_log: str) -> None:
        self._events.put(TurnEvent("tool", tool_log))


class Turn:
    """One agent turn: started on the background loop, drawn on the caller's.

    Iterate it to receive :class:`TurnEvent` objects as the agent produces
    them, then read :attr:`result` for the outcome::

        turn = Turn(agent, "deploy the api", thread_id=..., recursion_limit=25)
        for event in turn:
            placeholder.markdown(event.payload)   # on the script thread
        outcome = turn.result

    Iterating consumes the turn; :attr:`result` is only available afterwards.
    """

    def __init__(
        self,
        agent: Any,
        query: str,
        *,
        thread_id: str,
        recursion_limit: int,
        timeout_seconds: float | None = None,
        grace_seconds: float = 30.0,
    ) -> None:
        self._events: queue.Queue[TurnEvent] = queue.Queue()
        self._result: QueryResult | None = None
        # `run_query` owns the turn deadline so a cut-short turn still reports
        # its partial text and spent tokens. This one bounds the *wait* on this
        # side of the boundary — iteration and the final collect together — so
        # a loop that stops answering cannot hold the script thread, and the
        # browser with it, indefinitely. The grace is what separates the two:
        # under it, `run_query`'s own timeout wins and the partial turn is
        # reported normally.
        self._deadline = None if timeout_seconds is None else timeout_seconds + grace_seconds
        self._started = time.monotonic()
        self._future = asyncio.run_coroutine_threadsafe(
            run_query(
                agent,
                query,
                _QueuedRenderer(self._events),
                thread_id=thread_id,
                recursion_limit=recursion_limit,
                timeout_seconds=timeout_seconds,
            ),
            get_loop(),
        )

    def __iter__(self):
        """Yield events until the turn finishes or runs out of time.

        Every event carries the *full* accumulated text or tool log, never a
        delta, so when several have queued up only the last of each kind says
        anything the others do not. Dropping the rest is lossless and saves
        repainting the same placeholder once per streamed token.
        """
        while True:
            try:
                first = self._events.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                if self._future.done():
                    break
                if self._out_of_time():
                    # The deadline is only load-bearing here. Leaving it to
                    # `_collect` made it dead code: iteration ended only when
                    # the future finished, so a loop that never finished held
                    # the script thread and the browser with it, and `_collect`
                    # was never reached to notice.
                    break
                continue

            latest: dict[str, TurnEvent] = {first.kind: first}
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                latest[event.kind] = event

            # Tool output before text: within one batch the tool call happened
            # first, and the answer reads as a response to it.
            for kind in ("tool", "text"):
                if kind in latest:
                    yield latest[kind]

        self._result = self._collect()

    def _out_of_time(self) -> bool:
        """True once the whole turn has outlived its deadline."""
        remaining = self._remaining()
        return remaining is not None and remaining <= 0.0

    def _remaining(self) -> float | None:
        """Seconds left on the deadline, or None when there is no deadline."""
        if self._deadline is None:
            return None
        return self._deadline - (time.monotonic() - self._started)

    def _collect(self) -> QueryResult:
        """The turn's outcome, converting a stuck future into a reported error.

        The wait is what is *left* of the deadline, not the whole of it again:
        iteration has already spent most of it, and waiting a second full
        deadline here would double the time the page sits frozen.
        """
        remaining = self._remaining()
        try:
            return self._future.result(timeout=None if remaining is None else max(0.0, remaining))
        except TimeoutError:
            self._future.cancel()
            logger.error("Turn did not return within %ss", self._deadline)
            return QueryResult(
                error=f"The agent did not respond within {self._deadline:.0f} seconds."
            )
        except Exception as exc:
            # run_query catches its own failures, so reaching here means the
            # wiring around it broke — worth reporting with the actual reason
            # rather than as an empty string, which is how the render bug this
            # module exists to fix presented itself.
            logger.exception("Turn failed outside the agent")
            return QueryResult(error=f"{type(exc).__name__}: {exc}")

    @property
    def result(self) -> QueryResult:
        """The outcome. Available once iteration has finished."""
        if self._result is None:
            self._result = self._collect()
        return self._result
