"""MCP server (sprint 4): a thin, authenticated adapter over our own REST API.

Transport is Streamable HTTP mounted in-process at ``/mcp`` (main.py). The
transport app must serve at ``/`` internally so the client URL is exactly
``<host>/mcp`` once mounted. S4-S6 register the mirrored tool table and the
composite tools on the ``mcp`` singleton below.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp

from . import api
from .auth import McpAuthMiddleware

mcp = MCPServer(
    "reforger-manager",
    instructions=(
        "Administration interface for an Arma Reforger dedicated-server manager. "
        "Tools mirror the manager's REST API 1:1; state-changing tools take a "
        "confirm flag, and job-returning tools return a job id you can poll via "
        "wait_for_job."
    ),
)


@mcp.tool(description="Smoke tool: GET /api/health — returns {'status': 'ok'}.")
async def ping() -> dict[str, Any]:
    return await api.call("GET", "/api/health")


# The mirrored tool table: read tools (S4) + confirm-gated mutations (S5),
# then the composite read-only tools (S6) alongside it.
from .tools import register_tools  # noqa: E402  (after the singleton exists)

register_tools(mcp)

from .composites import register_composites  # noqa: E402  (after the singleton exists)

register_composites(mcp)


def mcp_app() -> ASGIApp:
    """The streamable-HTTP ASGI app wrapped in the bearer-token auth middleware.

    The transport app keeps the SDK's default inner path (``/mcp``), so main.py
    can register the wrapper as a plain Starlette ``Route("/mcp")`` — a Mount
    would never match the exact ``/mcp`` URL (Starlette compiles Mount paths to
    ``<path>/{path}``, i.e. it requires at least a trailing slash).

    SDK quirks (S4-S7 authors):
    - Every call returns a *fresh* Starlette app with a fresh, single-use
      session manager; the manager's lifespan (``session_manager.run()``) must
      be entered exactly once per instance. Production enters it in main.py's
      lifespan — Starlette does not propagate lifespan events to mounts or
      plain routes. Tests driving this app over ASGITransport must enter the
      inner app's lifespan themselves
      (``wrapped.app.router.lifespan_context(wrapped.app)``).
    - ``host="0.0.0.0"`` disables the SDK's auto DNS-rebinding protection
      (it only allows ``Host: 127.0.0.1`` / ``localhost``, which no real
      deployment hits behind a proxy); auth is enforced by the middleware.
    """
    return McpAuthMiddleware(mcp.streamable_http_app(host="0.0.0.0"))
