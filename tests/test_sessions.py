"""Long-lived MCP sessions: reuse, lifecycle, and failure reporting.

These use a fake client rather than real subprocesses. The behaviours that
needed real processes to discover — that a killed server surfaces as an empty
ClosedResourceError, and that a cancel scope must be exited by the task that
entered it — were established by fault injection against the shipped example
server; what is pinned here is that the handling of them stays in place.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from langchain_core.tools import ToolException, tool

from mcp_agent import sessions
from mcp_agent.sessions import SessionPool, _is_session_failure, close_pool, open_pool


@tool
def alpha(value: str) -> str:
    """First tool."""
    return value


@tool
def beta(value: str) -> str:
    """Second tool."""
    return value


class FakeClient:
    """Stands in for MultiServerMCPClient, tracking session lifecycle."""

    opened = 0
    closed = 0

    def __init__(self, config):
        self.config = config

    @asynccontextmanager
    async def session(self, server_name, **kwargs):
        FakeClient.opened += 1
        try:
            yield f"session-{server_name}"
        finally:
            FakeClient.closed += 1


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    FakeClient.opened = FakeClient.closed = 0
    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    async def fake_load(session, server_name=None):
        return [beta, alpha]  # deliberately unsorted

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load)
    yield
    await close_pool()


async def test_tools_are_sorted_for_a_stable_cache_prefix():
    """Tool order is part of the cached prefix; an unstable order misses."""
    pool = await open_pool({"a": {}})
    assert [t.name for t in pool.tools] == ["alpha", "beta"]


async def test_the_same_config_reuses_the_open_session():
    """Reuse is the point: reopening would pay startup again and leak the old."""
    first = await open_pool({"a": {}})
    second = await open_pool({"a": {}})
    assert first is second
    assert FakeClient.opened == 1


async def test_a_changed_config_closes_the_previous_session_first():
    """Otherwise every Apply Settings click leaks a server process."""
    await open_pool({"a": {}})
    await open_pool({"b": {}})
    assert FakeClient.closed == 1
    assert FakeClient.opened == 2


async def test_closing_releases_every_session():
    await open_pool({"a": {}, "b": {}})
    assert FakeClient.opened == 2
    await close_pool()
    assert FakeClient.closed == 2
    assert sessions.current_pool() is None


async def test_closing_twice_is_harmless():
    await open_pool({"a": {}})
    await close_pool()
    await close_pool()
    assert FakeClient.closed == 1


async def test_a_server_that_fails_to_start_does_not_take_the_others_with_it(
    monkeypatch,
):
    async def selective(session, server_name=None):
        if server_name == "broken":
            raise RuntimeError("could not launch")
        return [alpha]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", selective)

    pool = await open_pool({"broken": {}, "working": {}})
    assert [t.name for t in pool.tools] == ["alpha"]
    assert [(f.server_name, f.at_startup) for f in pool.failures] == [("broken", True)]
    assert pool.healthy is False


# --- Session death ------------------------------------------------------------


class Boom(Exception):
    """Named to match the transport-failure list, as anyio's error is."""


ClosedResourceError = type("ClosedResourceError", (Exception,), {})


async def test_a_dead_session_reports_something_a_person_can_act_on(monkeypatch):
    """The raw failure is ClosedResourceError with an empty message."""

    async def dies(*args, **kwargs):
        raise ClosedResourceError()

    async def fake_load(session, server_name=None):
        broken = alpha.model_copy()
        broken.coroutine = dies
        return [broken]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load)

    pool = await open_pool({"time": {}})
    with pytest.raises(ToolException) as caught:
        await pool.tools[0].ainvoke({"value": "x"})

    assert "no longer responding" in str(caught.value)
    assert "Apply Settings" in str(caught.value)
    assert pool.healthy is False
    assert [(f.server_name, f.at_startup) for f in pool.failures] == [("time", False)]


async def test_a_dead_session_is_recorded_once_not_per_call(monkeypatch):
    async def dies(*args, **kwargs):
        raise ClosedResourceError()

    async def fake_load(session, server_name=None):
        broken = alpha.model_copy()
        broken.coroutine = dies
        return [broken]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load)

    pool = await open_pool({"time": {}})
    for _ in range(3):
        with pytest.raises(ToolException):
            await pool.tools[0].ainvoke({"value": "x"})
    assert len(pool.failures) == 1


async def test_an_ordinary_tool_error_is_not_mistaken_for_a_dead_session(monkeypatch):
    """Only transport failures get the reconnect message."""

    async def raises(*args, **kwargs):
        raise ValueError("the tool itself rejected the input")

    async def fake_load(session, server_name=None):
        failing = alpha.model_copy()
        failing.coroutine = raises
        return [failing]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load)

    pool = await open_pool({"time": {}})
    with pytest.raises(ValueError, match="rejected the input"):
        await pool.tools[0].ainvoke({"value": "x"})
    assert pool.healthy is True


def test_transport_failures_are_recognised_through_a_cause_chain():
    outer = RuntimeError("wrapped")
    outer.__cause__ = ClosedResourceError()
    assert _is_session_failure(outer)
    assert not _is_session_failure(ValueError("ordinary"))


def test_a_self_referencing_cause_chain_does_not_hang():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert not _is_session_failure(a)


async def test_a_pool_that_never_started_closes_cleanly():
    await SessionPool(config={}).aclose()
