"""A minimal MCP server, for verifying that tool wiring works end to end.

Deliberately dependency-free beyond the `mcp` package this project already
installs, and it touches nothing outside the process. Use it to prove the agent
can discover and call a tool before pointing the app at a real server.

    uv run python examples/mcp_server_time.py     # speaks MCP over stdio

Registered in `example_config.json`; copy that to your MCP config to use it.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("time")


@mcp.tool()
def get_current_time(timezone: str = "UTC") -> str:
    """Return the current time in an IANA timezone, e.g. 'Asia/Seoul'.

    Args:
        timezone: IANA timezone name. Defaults to UTC.
    """
    if timezone not in available_timezones():
        return f"Unknown timezone {timezone!r}. Use an IANA name such as 'Asia/Seoul'."

    now = datetime.now(ZoneInfo(timezone))
    return now.strftime("%Y-%m-%d %H:%M:%S %Z (%z)")


if __name__ == "__main__":
    mcp.run(transport="stdio")
