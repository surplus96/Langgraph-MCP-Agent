"""Long-lived MCP sessions.

The adapter's default is a session per tool call. Measured on this project, that
costs about 610 ms per call, of which roughly 0.003 ms is the tool's actual
work — 78% of it is importing the MCP server's Pydantic models into a fresh
interpreter, which no amount of repetition warms up. A ReAct turn making a
dozen tool calls therefore pays around 7 seconds of pure overhead before the
model has said anything. Holding the session brings a call to about 11 ms.

The cost of that speed is lifecycle. A per-call session is self-healing: if a
server dies, the next call simply starts a new one. A held session is not — it
has to be noticed, reported and rebuilt. That is what most of this module is.

One structural constraint drives the design. The session is an async context
manager guarding an anyio cancel scope, and a scope must be exited by the task
that entered it. Entering it in one ``run_sync`` call and exiting in another
raises ``RuntimeError: Attempted to exit cancel scope in a different task`` —
and because the tool calls in between succeed, the failure surfaces only at
teardown, leaking the server process. So each session is owned end to end by
one long-lived "keeper" task, which is told to stop rather than cancelled.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool, ToolException

logger = logging.getLogger(__name__)

#: Exception names that mean the transport is gone rather than the tool
#: failing. Matched by name because they come from anyio and the MCP SDK's
#: internals, which are not part of either package's public surface.
_SESSION_FAILURE_NAMES = frozenset(
    {
        "ClosedResourceError",
        "BrokenResourceError",
        "EndOfStream",
        "BrokenPipeError",
        "ConnectionResetError",
    }
)


def _is_session_failure(exc: BaseException) -> bool:
    """True when the transport died, as opposed to the tool raising."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _SESSION_FAILURE_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass
class ServerFailure:
    """A server that could not be reached, or that died while held open."""

    server_name: str
    error: str
    #: True if it failed at startup, False if it died mid-session.
    at_startup: bool


@dataclass
class SessionPool:
    """One long-lived MCP session per configured server.

    Not thread-safe by design: every method must run on the single background
    loop from :mod:`mcp_agent.runtime`, which is also where the keeper tasks
    live. Sharing it across loops would reintroduce exactly the cancel-scope
    problem it exists to avoid.
    """

    config: dict[str, Any]
    tools: list[BaseTool] = field(default_factory=list)
    failures: list[ServerFailure] = field(default_factory=list)
    _stop: asyncio.Event | None = None
    _keepers: list[asyncio.Task[None]] = field(default_factory=list)

    async def start(self) -> None:
        """Open a session per server and collect their tools.

        A server that fails to start is recorded and skipped rather than
        failing the whole pool: one broken entry in the config should not cost
        the user every other tool.
        """
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(self.config)
        self._stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        ready: list[tuple[str, asyncio.Future[list[BaseTool]]]] = []
        for server_name in self.config:
            future: asyncio.Future[list[BaseTool]] = loop.create_future()
            keeper = asyncio.create_task(
                self._keep(client, server_name, future, self._stop),
                name=f"mcp-session-{server_name}",
            )
            self._keepers.append(keeper)
            ready.append((server_name, future))

        collected: list[BaseTool] = []
        for server_name, future in ready:
            try:
                collected.extend(await future)
            except Exception as exc:
                logger.warning("MCP server %r failed to start: %s", server_name, exc)
                self.failures.append(
                    ServerFailure(server_name=server_name, error=str(exc), at_startup=True)
                )

        # Deterministic order keeps the tool-definition block byte-stable, which
        # is what makes the cached prompt prefix reusable across turns.
        self.tools = sorted(collected, key=lambda tool: tool.name)

    async def _keep(
        self,
        client: Any,
        server_name: str,
        ready: asyncio.Future[list[BaseTool]],
        stop: asyncio.Event,
    ) -> None:
        """Own one session for its whole life.

        Enters and exits the session in this task, publishes the tools once,
        then does nothing until told to stop. Everything after ``ready`` is
        resolved is teardown or failure reporting.
        """
        from langchain_mcp_adapters.tools import load_mcp_tools

        try:
            async with client.session(server_name) as session:
                tools = await load_mcp_tools(session, server_name=server_name)
                ready.set_result([self._guard(tool, server_name) for tool in tools])
                await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                # The session was live and broke. The tools bound to it will now
                # fail, and nothing else would say why.
                logger.warning("MCP session %r ended: %s", server_name, exc)
                self.failures.append(
                    ServerFailure(server_name=server_name, error=str(exc), at_startup=False)
                )

    def _guard(self, tool: BaseTool, server_name: str) -> BaseTool:
        """Wrap a tool so a dead session reports itself usefully.

        A held session does not announce its own death: the keeper is parked
        waiting to be stopped, not reading the stream, so killing the server
        leaves everything looking fine until something calls a tool. Verified
        by killing the process — the raw failure that reaches the model is
        ``ClosedResourceError`` with an empty message, which tells nobody
        anything. Catch it at the one place it is observable.
        """
        # `coroutine` lives on StructuredTool rather than the BaseTool the
        # adapter is typed to return, so reach for it defensively: a tool
        # without one is left alone rather than half-wrapped.
        inner = getattr(tool, "coroutine", None)
        if inner is None:
            return tool

        async def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return await inner(*args, **kwargs)
            except Exception as exc:
                if not _is_session_failure(exc):
                    raise
                message = (
                    f"The MCP server {server_name!r} is no longer responding; its "
                    "connection has closed. Click 'Apply Settings' to reconnect."
                )
                if not any(f.server_name == server_name for f in self.failures):
                    logger.warning("MCP session %r is dead: %r", server_name, exc)
                    self.failures.append(
                        ServerFailure(
                            server_name=server_name,
                            error=f"{type(exc).__name__}: {exc}".rstrip(": "),
                            at_startup=False,
                        )
                    )
                raise ToolException(message) from exc

        tool.coroutine = call
        return tool

    @property
    def healthy(self) -> bool:
        """False once any server has failed to start or has died."""
        return not self.failures

    async def aclose(self) -> None:
        """Signal every keeper to stop and wait for the processes to go.

        Signalled rather than cancelled: a cancellation delivered from another
        task unwinds the scope from the wrong place. Setting the event lets each
        keeper fall out of its own ``async with``.
        """
        if self._stop is not None:
            self._stop.set()
        if self._keepers:
            await asyncio.gather(*self._keepers, return_exceptions=True)
        self._keepers.clear()
        self.tools = []


#: The pool for the current configuration. Process-wide, because the sessions
#: are subprocesses of this process and the loop that owns them is too.
_pool: SessionPool | None = None


async def open_pool(config: dict[str, Any]) -> SessionPool:
    """Return a pool for ``config``, reusing the open one when it still matches.

    Reuse is the whole point: rebuilding on an unrelated setting change would
    pay the startup cost again and, worse, leave the previous server processes
    running if the old pool were merely dropped.
    """
    global _pool

    if _pool is not None and _pool.config == config:
        return _pool

    await close_pool()
    pool = SessionPool(config=config)
    await pool.start()
    _pool = pool
    return pool


async def close_pool() -> None:
    """Close the open pool, if any. Safe to call when there is none."""
    global _pool

    if _pool is not None:
        await _pool.aclose()
        _pool = None


def current_pool() -> SessionPool | None:
    """The open pool, for reporting. Does not start one."""
    return _pool
