"""One version, in one place.

`__version__` was hand-maintained and said 0.3.0 while `pyproject.toml` said
0.4.1 — two releases of drift, unnoticed because nothing imports it. Deriving
it from the installed distribution is only half the fix; this is the half that
fails when the two disagree.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import mcp_agent

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _declared_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_the_package_reports_the_version_it_was_built_with():
    assert mcp_agent.__version__ == _declared_version()


def test_the_container_image_is_tagged_with_the_same_version():
    """A compose file pinning a stale tag runs the previous release."""
    compose = (Path(__file__).parent.parent / "dockers" / "docker-compose.yaml").read_text(
        encoding="utf-8"
    )
    assert f"langgraph-mcp-agent:{_declared_version()}" in compose, compose
