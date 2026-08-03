"""Check the health tool on an already-running HarborRAG MCP server.

Connects as a real client to the server's HTTP (streamable-http) endpoint -
started separately via scripts/deployment/start_mcp_server.sh - and calls
harborrag_health_check over that live connection. No mocked transport, no
stubbed tool: if the server isn't running, this fails to connect.

Usage:
    scripts/deployment/start_mcp_server.sh &   # start the server, keep it running
    uv run python scripts/check_mcp_server_health.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from fastmcp import Client

_HEALTH_TOOL = "harborrag_health_check"


def _read_args() -> tuple[str, str, float, float]:
    parser = argparse.ArgumentParser(description="Check MCP health tool over HTTP transport")
    parser.add_argument("--host", default=os.environ.get("HARBORRAG_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.environ.get("HARBORRAG_MCP_PORT", "8765"))
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("HARBORRAG_MCP_HEALTH_TIMEOUT", "30")),
        help="Total seconds to keep retrying before failing",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("HARBORRAG_MCP_HEALTH_INTERVAL", "1")),
        help="Seconds between retries",
    )
    args = parser.parse_args()
    return args.host, str(args.port), max(args.timeout, 0.0), max(args.interval, 0.1)


async def main() -> None:
    host, port, timeout_seconds, interval_seconds = _read_args()
    url = f"http://{host}:{port}/mcp"
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = last_error or "unknown connection error"
            print(f"Could not reach the MCP server at {url}: {reason}", file=sys.stderr)
            print(
                "Start it first with scripts/deployment/start_mcp_server.sh",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            async with asyncio.timeout(remaining):
                async with Client(url) as client:
                    tools = await client.list_tools()
                    names = {tool.name for tool in tools}
                    if _HEALTH_TOOL not in names:
                        print(
                            f"{_HEALTH_TOOL} missing from server tools: {sorted(names)}",
                            file=sys.stderr,
                        )
                        sys.exit(1)

                    result = await client.call_tool(_HEALTH_TOOL, {}, raise_on_error=False)
                    if result.is_error:
                        print(f"{_HEALTH_TOOL} reported an error: {result.data}", file=sys.stderr)
                        sys.exit(1)

            print(f"{url} is up and {_HEALTH_TOOL} responded ok.")
            return
        except TimeoutError:
            last_error = f"timed out after {timeout_seconds:.1f}s"
        except OSError as exc:
            last_error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive fallback for transport errors
            last_error = str(exc)

        remaining_after_attempt = deadline - time.monotonic()
        if remaining_after_attempt <= 0:
            reason = last_error or "unknown connection error"
            print(f"Could not reach the MCP server at {url}: {reason}", file=sys.stderr)
            print(
                "Start it first with scripts/deployment/start_mcp_server.sh",
                file=sys.stderr,
            )
            sys.exit(1)

        await asyncio.sleep(min(interval_seconds, remaining_after_attempt))


if __name__ == "__main__":
    asyncio.run(main())
