"""LangGraph ReAct agent with MCP tool adapters."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    # Read from the installed distribution rather than repeating the number.
    # Hand-maintained, this drifted two releases without anything noticing:
    # nothing imports it, so nothing broke, and the only reader was a person
    # trusting it. `tests/test_version.py` is what stops that recurring.
    __version__ = version("langgraph-mcp-agents")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"
