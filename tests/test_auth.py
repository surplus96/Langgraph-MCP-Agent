"""The login gate, including the empty-credential bypass it used to have."""

from __future__ import annotations

import pytest

from mcp_agent import auth


def test_login_disabled_by_default(monkeypatch):
    monkeypatch.delenv("USE_LOGIN", raising=False)
    assert auth.login_enabled() is False


def test_login_enabled_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("USE_LOGIN", "TRUE")
    assert auth.login_enabled() is True


def test_correct_credentials_verify(monkeypatch):
    monkeypatch.setenv("USER_ID", "alice")
    monkeypatch.setenv("USER_PASSWORD", "correct horse battery staple")
    assert auth.verify("alice", "correct horse battery staple") is True


def test_wrong_password_is_rejected(monkeypatch):
    monkeypatch.setenv("USER_ID", "alice")
    monkeypatch.setenv("USER_PASSWORD", "s3cret")
    assert auth.verify("alice", "wrong") is False


def test_empty_submission_is_rejected_even_when_expected_is_empty(monkeypatch):
    """Compose turns an unset variable into "", which used to authenticate anyone."""
    monkeypatch.setenv("USER_ID", "")
    monkeypatch.setenv("USER_PASSWORD", "")
    assert auth.verify("", "") is False


def test_blank_configured_credentials_raise(monkeypatch):
    monkeypatch.setenv("USER_ID", "")
    monkeypatch.setenv("USER_PASSWORD", "")
    with pytest.raises(auth.AuthConfigError):
        auth.verify("someone", "something")


def test_unset_configured_credentials_raise(monkeypatch):
    monkeypatch.delenv("USER_ID", raising=False)
    monkeypatch.delenv("USER_PASSWORD", raising=False)
    with pytest.raises(auth.AuthConfigError):
        auth.verify("someone", "something")
