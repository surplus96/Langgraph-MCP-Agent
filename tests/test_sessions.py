"""Long-lived MCP sessions: reuse, lifecycle, and failure reporting.

These use a fake client rather than real subprocesses. The behaviours that
needed real processes to discover — that a killed server surfaces as an empty
ClosedResourceError, and that a cancel scope must be exited by the task that
entered it — were established by fault injection against the shipped example
server; what is pinned here is that the handling of them stays in place.
"""

from __future__ import annotations

import asyncio
import re
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

    async def fake_load(session, server_name=None, **kwargs):
        return [beta, alpha]  # deliberately unsorted

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load)
    yield
    await close_pool()


async def test_tools_are_sorted_for_a_stable_cache_prefix():
    """Tool order is part of the cached prefix; an unstable order misses."""
    pool = await open_pool({"a": {}})
    assert [t.name for t in pool.tools] == ["a_alpha", "a_beta"]


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
    async def selective(session, server_name=None, **kwargs):
        if server_name == "broken":
            raise RuntimeError("could not launch")
        return [alpha]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", selective)

    pool = await open_pool({"broken": {}, "working": {}})
    assert [t.name for t in pool.tools] == ["working_alpha"]
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

    async def fake_load(session, server_name=None, **kwargs):
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

    async def fake_load(session, server_name=None, **kwargs):
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

    async def fake_load(session, server_name=None, **kwargs):
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


# --- Bounding the call, not just the turn -------------------------------------


def test_tool_timeout_defaults_and_is_configurable(monkeypatch):
    from mcp_agent.sessions import DEFAULT_TOOL_TIMEOUT, tool_timeout

    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    assert tool_timeout() == DEFAULT_TOOL_TIMEOUT

    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "5")
    assert tool_timeout() == 5.0


@pytest.mark.parametrize("bad", ["", "soon", "0", "-3"])
def test_a_useless_tool_timeout_falls_back(monkeypatch, bad):
    """A typo must not disable the bound that keeps threads valid."""
    from mcp_agent.sessions import DEFAULT_TOOL_TIMEOUT, tool_timeout

    monkeypatch.setenv("MCP_TOOL_TIMEOUT", bad)
    assert tool_timeout() == DEFAULT_TOOL_TIMEOUT


def _guarded(coroutine, *, name="slow_tool"):
    """Run one tool through _guard, as the agent would."""
    from langchain_core.tools import StructuredTool

    from mcp_agent.sessions import SessionPool

    tool = StructuredTool.from_function(
        coroutine=coroutine, name=name, description="x", args_schema=None
    )
    return SessionPool(config={})._guard(tool, "server-a")


def test_a_slow_tool_becomes_a_tool_error_rather_than_hanging(monkeypatch):
    """The guarantee: the call is bounded, so the tool_use/tool_result pair closes.

    Left unbounded, the turn deadline lands mid-call and the thread is
    checkpointed with tool_calls and no ToolMessage — an invalid sequence that
    every later turn on that thread rebuilds.
    """
    from langchain_core.tools import ToolException

    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "0.05")

    async def never_returns():
        await asyncio.sleep(30)

    guarded = _guarded(never_returns)

    with pytest.raises(ToolException) as caught:
        asyncio.run(guarded.coroutine())

    assert "did not finish" in str(caught.value)
    assert "slow_tool" in str(caught.value)


def test_a_tool_that_finishes_in_time_is_untouched(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "5")

    async def quick():
        return "done"

    assert asyncio.run(_guarded(quick).coroutine()) == "done"


def test_an_ordinary_tool_failure_still_propagates(monkeypatch):
    """Only transport death and timeouts are translated; real errors are real."""
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "5")

    async def explodes():
        raise ValueError("the file does not exist")

    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(_guarded(explodes).coroutine())


# --- Tool names across servers ------------------------------------------------


async def test_tool_names_are_namespaced_by_server(monkeypatch):
    """Without the prefix two servers exposing `search` collide.

    Verified against the real `create_agent`: three tools in, two bound, and
    the loser never reaches the model while the sidebar still counts it. The
    fake loader here cannot reproduce that, so what this pins is that the tools
    the pool hands out carry the server name.
    """
    pool = await open_pool({"github": {}})
    assert [t.name for t in pool.tools] == ["github_alpha", "github_beta"]


async def test_renaming_does_not_disturb_the_call(monkeypatch):
    """The MCP server is still asked for the tool by its own name.

    Verified in the adapter's source as well as here: it closes over the *MCP*
    tool's name when building the call, never the LangChain tool's, so the
    rename is visible to the model and to nobody else. The stub closes over the
    unprefixed name the same way; if renaming ever rewrote the call, the model
    would see `github_alpha` and the server would be asked for a tool it does
    not have.
    """
    called_as = "alpha"

    async def echo(value: str) -> str:
        return f"{called_as}:{value}"

    async def fake_load(session, server_name=None, **kwargs):
        renamable = alpha.model_copy()
        renamable.coroutine = echo
        return [renamable]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load)

    pool = await open_pool({"github": {}})
    assert pool.tools[0].name == "github_alpha"
    assert await pool.tools[0].ainvoke({"value": "x"}) == "alpha:x"


async def test_tools_that_still_collide_are_reported(monkeypatch):
    """Prefixing makes this unreachable normally; silence would not be.

    `create_agent` binds one of a duplicated name and drops the rest, so a
    collision that survives prefixing costs the user a tool the sidebar says
    they have, with nothing said about it.
    """

    async def colliding(session, server_name=None, **kwargs):
        first = alpha.model_copy()
        second = alpha.model_copy()
        return [first, second]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", colliding)

    pool = await open_pool({"one": {}})
    assert pool.healthy is False
    assert any("named 'one_alpha'" in failure.error for failure in pool.failures), pool.failures


async def test_distinct_tool_names_are_not_reported(monkeypatch):
    pool = await open_pool({"a": {}})
    assert pool.healthy is True
    assert pool.failures == []


# --- Building a tool name the API will accept ---------------------------------


def test_a_plain_name_is_just_prefixed():
    from mcp_agent.sessions import namespaced

    assert namespaced("github", "search") == "github_search"


def test_a_smithery_style_key_does_not_produce_an_illegal_name():
    """`README.md` tells users to paste Smithery JSON, whose keys look like this.

    Anthropic rejects the whole request when any tool name fails
    `^[a-zA-Z0-9_-]{1,64}$` — so one such entry costs every turn, not one tool.
    """
    from mcp_agent.sessions import namespaced

    name = namespaced("@smithery-ai/server-sequential-thinking", "sequentialthinking")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name), name


@pytest.mark.parametrize(
    "server_name",
    ["@scope/pkg", "server name", "서버", "a.b.c", "x" * 200, "", "///", "-"],
)
def test_every_server_name_yields_an_acceptable_tool_name(server_name):
    from mcp_agent.sessions import namespaced

    name = namespaced(server_name, "search")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name), name


def test_the_tool_name_is_kept_whole_when_the_prefix_will_not_fit():
    """The model reasons about the tool name; the prefix only disambiguates."""
    from mcp_agent.sessions import namespaced

    tool = "b" * 60
    assert namespaced("a" * 40, tool).endswith(tool)
    assert len(namespaced("a" * 40, tool)) <= 64


def test_a_tool_name_longer_than_the_limit_is_cut_too():
    """The prefix giving way is not enough when the tool name alone is over.

    Nothing of the prefix fits here, so the last thing standing between the
    server and a rejected request is the cap on the tool name itself.
    """
    from mcp_agent.sessions import namespaced

    assert namespaced("server", "t" * 80) == "t" * 64


def test_truncating_the_prefix_does_not_leave_a_trailing_underscore():
    """Truncation is the one step that can create one, so it strips after.

    A prefix cut mid-word at an underscore would otherwise join as `a__tool`,
    which is legal but reads as a mistake and is not the name anything else
    would predict.
    """
    from mcp_agent.sessions import namespaced

    tool = "t" * 50  # leaves room for 13 characters of prefix
    assert namespaced("a" * 12 + "_" + "b" * 30, tool) == "a" * 12 + "_" + tool


def test_a_tool_name_the_api_would_reject_is_also_cleaned():
    """The name comes from the server, which is no more trusted than the config."""
    from mcp_agent.sessions import namespaced

    assert namespaced("github", "search files!") == "github_search_files"


@pytest.mark.parametrize(
    ("server_name", "tool_name", "expected"),
    [
        ("@scope/pkg", "search", "scope_pkg_search"),
        (
            "@smithery-ai/server-sequential-thinking",
            "think",
            "smithery-ai_server-sequential-thinking_think",
        ),
        ("", "search", "search"),
        ("///", "search", "search"),
        ("-", "search", "-_search"),
        ("a.b.c", "search", "a_b_c_search"),
        ("github", "", "github_tool"),
        ("github", "!!!", "github_tool"),
        ("서버", "검색", "tool"),
    ],
)
def test_what_a_name_actually_becomes(server_name, tool_name, expected):
    """Exact, not just "matches the pattern".

    The regex admits `_scope_pkg_search`, `__search` and `github_` — so three
    ways of getting this wrong pass a pattern check and are only visible when
    the whole string is written down. The last row is the one that costs
    something real: a server whose tool names are entirely non-ASCII collapses
    every one of them to the same name, which is why the collision report below
    has to work.
    """
    from mcp_agent.sessions import namespaced

    assert namespaced(server_name, tool_name) == expected


async def test_two_servers_shortened_alike_are_blamed_by_name(monkeypatch):
    """The remedy is in the config, not at the server, and has to say so.

    Two long server names shorten to the same prefix and every tool from both
    collides. Telling the user to "rename them at the server" sends them to fix
    something that is not broken.
    """
    pool = await open_pool({"s" * 70: {}, "s" * 80: {}})

    assert pool.healthy is False
    reported = " ".join(failure.error for failure in pool.failures)
    assert "MCP configuration" in reported, pool.failures
    assert all(failure.server_name.count(",") == 1 for failure in pool.failures), pool.failures


async def test_one_server_colliding_with_itself_is_blamed_at_the_server(monkeypatch):
    """Two tool names that differ only outside `[a-zA-Z0-9_-]` arrive identical."""

    async def korean(session, server_name=None, **kwargs):
        first = alpha.model_copy(update={"name": "검색"})
        second = alpha.model_copy(update={"name": "도구"})
        return [first, second]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", korean)

    pool = await open_pool({"papers": {}})

    assert [t.name for t in pool.tools] == ["papers_tool", "papers_tool"]
    assert pool.healthy is False
    assert [f.server_name for f in pool.failures] == ["papers"], pool.failures
    assert "Rename them at the server" in pool.failures[0].error
