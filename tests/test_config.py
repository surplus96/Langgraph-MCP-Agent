"""Config loading, validation and persistence."""

from __future__ import annotations

import json

import pytest

from mcp_agent.config import (
    ConfigError,
    load_config,
    save_config,
    validate_config,
    validate_server_config,
)


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_CONFIG_PATH", str(tmp_path / "config.json"))


def test_missing_file_yields_default():
    assert load_config() == {}


def test_round_trip():
    config = {"time": {"command": "python", "args": ["srv.py"], "transport": "stdio"}}
    save_config(config)
    assert load_config() == config


def test_corrupt_file_raises_rather_than_silently_defaulting(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text('{"a": ,}', encoding="utf-8")
    monkeypatch.setenv("MCP_CONFIG_PATH", str(path))

    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config()


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MCP_CONFIG_PATH", str(path))

    save_config({"a": {"command": "npx", "args": [], "transport": "stdio"}})

    assert json.loads(path.read_text())
    assert list(tmp_path.glob("*.tmp")) == []


def test_url_entry_infers_sse_transport():
    entry = validate_server_config("remote", {"url": "https://example.test/mcp"})
    assert entry["transport"] == "sse"


def test_command_entry_defaults_to_stdio():
    entry = validate_server_config("local", {"command": "npx", "args": []})
    assert entry["transport"] == "stdio"


def test_command_outside_allowlist_is_rejected():
    with pytest.raises(ConfigError, match="not permitted"):
        validate_server_config("evil", {"command": "bash", "args": ["-c", "id"]})


def test_allowlist_is_configurable(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_COMMANDS", "bash")
    entry = validate_server_config("ok", {"command": "bash", "args": []})
    assert entry["command"] == "bash"


def test_entry_without_command_or_url_is_rejected():
    with pytest.raises(ConfigError, match="either a 'command' or a 'url'"):
        validate_server_config("bad", {"transport": "stdio"})


def test_args_must_be_a_list():
    with pytest.raises(ConfigError, match="must be an array"):
        validate_server_config("bad", {"command": "npx", "args": "-y"})


def test_command_requires_args():
    with pytest.raises(ConfigError, match="requires an 'args' field"):
        validate_server_config("bad", {"command": "npx"})


def test_validate_config_checks_every_entry():
    with pytest.raises(ConfigError):
        validate_config(
            {
                "good": {"command": "npx", "args": []},
                "bad": {"command": "bash", "args": []},
            }
        )
