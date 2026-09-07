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
import os
import re
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


#: Seconds a single tool call may take before it is cut short. Must stay
#: comfortably below the turn timeout, or the turn deadline fires first and
#: takes the thread down with it — see :meth:`SessionPool._guard`.
DEFAULT_TOOL_TIMEOUT = 60.0


def tool_timeout() -> float:
    """How long one tool call may run. Override with MCP_TOOL_TIMEOUT."""
    raw = os.environ.get("MCP_TOOL_TIMEOUT")
    if not raw:
        return DEFAULT_TOOL_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning("MCP_TOOL_TIMEOUT=%r is not a number; using %s", raw, DEFAULT_TOOL_TIMEOUT)
        return DEFAULT_TOOL_TIMEOUT
    if value <= 0:
        logger.warning("MCP_TOOL_TIMEOUT=%r is not positive; using %s", raw, DEFAULT_TOOL_TIMEOUT)
        return DEFAULT_TOOL_TIMEOUT
    return value


#: Anthropic rejects a *request* — not just the offending tool — whose tool
#: names do not match ``^[a-zA-Z0-9_-]{1,64}$``. The prefix is built from a key
#: in a config file the user pastes into, and Smithery's own snippets use keys
#: like ``@smithery-ai/server-sequential-thinking``, so trusting it would turn
#: one bad config entry into every turn failing. Made safe here instead.
MAX_TOOL_NAME_LENGTH = 64
_UNSAFE_IN_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


def namespaced(server_name: str, tool_name: str) -> str:
    """``server_tool``, made safe to send and short enough to be accepted.

    The tool's own name is kept whole and the server prefix gives way, because
    the name is what the model reasons about; the prefix only disambiguates.
    A prefix truncated to nothing, or two servers truncated to the same thing,
    can still collide — :meth:`SessionPool._warn_about_duplicates` is what
    says so.
    """
    tool = _UNSAFE_IN_TOOL_NAME.sub("_", tool_name).strip("_") or "tool"
    tool = tool[:MAX_TOOL_NAME_LENGTH]

    # One strip, after the truncation rather than before it: stripping first is
    # redundant with this, and truncation is the only step that can *create* a
    # trailing underscore.
    prefix = _UNSAFE_IN_TOOL_NAME.sub("_", server_name)
    room = MAX_TOOL_NAME_LENGTH - len(tool) - 1
    prefix = prefix[:room].strip("_") if room > 0 else ""
    return f"{prefix}_{tool}" if prefix else tool


def _renamed(tool: BaseTool, name: str) -> BaseTool:
    """A copy of ``tool`` under ``name``.

    Only the LangChain-side name changes. The adapter closes over the MCP
    tool's own name for the actual call, so renaming here is invisible to the
    server and visible only to the model.
    """
    if tool.name == name:
        return tool
    return tool.model_copy(update={"name": name})


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
        # Which server each final name came from, so a collision can name the
        # servers to fix rather than repeating the tool's own name back.
        origins: dict[str, list[str]] = {}
        for server_name, future in ready:
            try:
                tools = await future
            except Exception as exc:
                logger.warning("MCP server %r failed to start: %s", server_name, exc)
                self.failures.append(
                    ServerFailure(server_name=server_name, error=str(exc), at_startup=True)
                )
                continue
            collected.extend(tools)
            for tool in tools:
                origins.setdefault(tool.name, []).append(server_name)

        # Deterministic order keeps the tool-definition block byte-stable, which
        # is what makes the cached prompt prefix reusable across turns.
        self.tools = sorted(collected, key=lambda tool: tool.name)
        self._warn_about_duplicates(origins)

    def _warn_about_duplicates(self, origins: dict[str, list[str]]) -> None:
        """Say so if two tools still share a name, and say where to fix it.

        Server prefixes make this unreachable for ordinary configurations, but
        three routes survive them: a server whose own tool names already carry
        another server's prefix; two server names `namespaced` had to shorten
        to the same thing; and a server whose tool names are entirely outside
        `[a-zA-Z0-9_-]`, which collapses every one of them to the same cleaned
        name. The failure is silent otherwise: `create_agent` binds one and
        drops the rest, so the model never sees a tool the sidebar counts.

        The remedy differs by route, which is why the servers are tracked. Two
        servers shortened together are fixed in the configuration; one server
        colliding with itself has to be fixed at the server.
        """
        for name, servers in origins.items():
            if len(servers) < 2:
                continue

            logger.warning(
                "%d tools are named %r (from %s); only one will reach the model",
                len(servers),
                name,
                ", ".join(sorted(set(servers))),
            )

            distinct = sorted(set(servers))
            if len(distinct) > 1:
                blamed = ", ".join(distinct)
                remedy = (
                    f"The server names {blamed} produce the same tool prefix. "
                    "Rename one of them in your MCP configuration."
                )
            else:
                blamed = distinct[0]
                remedy = (
                    f"They all come from {blamed!r}. Rename them at the server — note that "
                    "names outside [a-zA-Z0-9_-] are rewritten before use, so names that "
                    "differ only outside that set arrive here identical."
                )

            self.failures.append(
                ServerFailure(
                    server_name=blamed,
                    error=f"{len(servers)} registered tools are named {name!r}. {remedy}",
                    at_startup=True,
                )
            )

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
                # Namespacing: without it two servers exposing `search` collide,
                # and `create_agent` binds only the last one — the other never
                # reaches the model while the sidebar still counts it. Verified:
                # three tools in, two bound. The prefix also tells the model
                # which server a tool belongs to.
                #
                # Done here rather than with the adapter's `tool_name_prefix`,
                # which pastes the config key on unchecked. See `namespaced`.
                tools = await load_mcp_tools(session, server_name=server_name)
                ready.set_result(
                    [
                        self._guard(_renamed(tool, namespaced(server_name, tool.name)), server_name)
                        for tool in tools
                    ]
                )
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
        """Wrap a tool so it always returns something the thread can survive.

        Two failures are caught here, and both are about the same thing: a tool
        call that does not come back leaves the conversation broken, not just
        the turn.

        **A dead session does not announce itself.** The keeper is parked
        waiting to be stopped, not reading the stream, so killing the server
        leaves everything looking fine until something calls a tool. Verified
        by killing the process — the raw failure that reaches the model is
        ``ClosedResourceError`` with an empty message, which tells nobody
        anything.

        **A slow tool poisons the thread.** ``run_query`` bounds the *turn*. If
        that deadline lands while a tool is still running, the model node has
        already been checkpointed with its ``tool_calls`` and no
        ``ToolMessage`` ever follows — an invalid message sequence that
        Anthropic rejects. Because it is checkpointed, every later turn on that
        thread rebuilds the same invalid sequence, and the only way out is to
        reset the conversation. Bounding the *call* instead keeps the pair
        complete: ``ToolException`` reaches ``ToolNode``'s error handling and
        becomes an ordinary tool result the model can read and react to.
        """
        # `coroutine` lives on StructuredTool rather than the BaseTool the
        # adapter is typed to return, so reach for it defensively: a tool
        # without one is left alone rather than half-wrapped.
        inner = getattr(tool, "coroutine", None)
        if inner is None:
            return tool

        limit = tool_timeout()

        async def call(*args: Any, **kwargs: Any) -> Any:
            try:
                async with asyncio.timeout(limit):
                    return await inner(*args, **kwargs)
            except TimeoutError as exc:
                logger.warning("Tool %r on %r exceeded %ss", tool.name, server_name, limit)
                raise ToolException(
                    f"The tool {tool.name!r} did not finish within {limit:.0f} seconds and "
                    "was stopped. Tell the user it timed out rather than retrying it "
                    "unchanged."
                ) from exc
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
