"""Shared isolation for every test in this suite.

The one thing in here is not a convenience: without it, a developer who
follows the documented setup cannot run the tests.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ignore_the_developers_dotenv(monkeypatch):
    """Keep a real `.env` out of the tests.

    `app.py` calls `load_dotenv(override=False)`, and `find_dotenv` walks up
    from `app.py` itself rather than the working directory — so `chdir` does
    not escape it and neither does `monkeypatch.delenv`. The delenv happens
    first, which leaves the variable *unset*, which is exactly the case
    `override=False` fills in from the file.

    The result: `README.md` tells you to `cp .env.example .env`, that file
    ships `USE_LOGIN=true`, and every `AppTest` then stops at the login gate
    with no widgets drawn. Measured on a clean checkout: 39 of 48 tests in
    `test_app_smoke.py` fail with the file present and all 48 pass without it.
    CI never saw it because `.env` is gitignored, so the first person to hit
    this was a user following our own instructions.

    Tests own their environment. Anything a test needs, it sets.
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
