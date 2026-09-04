"""Credential checking for the optional login gate."""

from __future__ import annotations

import hmac
import os


class AuthConfigError(Exception):
    """Raised when the login gate is enabled but not usable."""


def login_enabled() -> bool:
    return os.environ.get("USE_LOGIN", "false").strip().lower() == "true"


def expected_credentials() -> tuple[str, str]:
    """Return the configured username and password.

    Raises:
        AuthConfigError: if either is missing or blank. Docker Compose turns an
            unset variable into an empty string, so without this check a blank
            login form satisfied ``"" == ""`` and authenticated anyone.
    """
    user = os.environ.get("USER_ID") or ""
    password = os.environ.get("USER_PASSWORD") or ""

    if not user or not password:
        raise AuthConfigError(
            "USE_LOGIN is enabled but USER_ID and/or USER_PASSWORD are unset or empty. "
            "Set both to non-empty values, or set USE_LOGIN=false."
        )
    return user, password


def verify(username: str, password: str) -> bool:
    """Check submitted credentials in constant time.

    Empty submissions are rejected before comparison.

    Raises:
        AuthConfigError: if the configured credentials are unusable.
    """
    if not username or not password:
        return False

    expected_user, expected_password = expected_credentials()
    user_ok = hmac.compare_digest(username, expected_user)
    password_ok = hmac.compare_digest(password, expected_password)
    return user_ok and password_ok
