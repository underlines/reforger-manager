"""Composite read-only tools (sprint 4, S6): small agent conveniences built
**only** on :func:`app.mcp.api.call` — no business logic, no second code path,
no direct DB access (every datum these need is carried by an existing
endpoint; even the supervisor status is visible as the server definition whose
``is_running`` flag the supervisor maintains).

- ``wait_for_job`` — poll one job until it reaches a terminal state and return
  the final record (``result``/``error``/``log_tail`` included); on timeout it
  returns the current snapshot with a ``timed_out: true`` marker instead of
  raising, so an agent can simply call it again.
- ``tail_log`` — slice the console-log endpoint down to the last ``lines``
  entries (clamped 1..1000).
- ``server_overview`` — servers + running id + engine row + 5 most recent
  jobs in one response, the "where do things stand" starting point.

Like the mirrored table, each tool returns a single JSON text block encoded
with ``pydantic_core.to_json`` and surfaces API failures as a deterministic
``ToolError`` carrying the HTTP status and detail.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic_core import to_json

from ..models import TERMINAL_JOB_STATES
from . import api


async def _api(method: str, path: str, **kwargs: Any) -> Any:
    """``api.call`` with the table's deterministic error mapping."""
    try:
        return await api.call(method, path, **kwargs)
    except api.ApiError as exc:
        raise ToolError(f"API {exc.status_code}: {exc.detail}") from exc


async def wait_for_job(job_id: int, timeout_s: int = 60, poll_s: float = 1.0) -> str:
    """Block until the job terminates, then return its final JobOut.

    On timeout the current non-terminal snapshot is returned with a
    ``timed_out: true`` marker — never an error — so the caller can simply
    poll again.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(0.01, float(poll_s))
    while True:
        job = await _api("GET", f"/api/jobs/{job_id}")
        if job.get("state") in TERMINAL_JOB_STATES:
            return to_json(job, fallback=str, indent=2).decode()
        if time.monotonic() >= deadline:
            return to_json({**job, "timed_out": True}, fallback=str, indent=2).decode()
        await asyncio.sleep(interval)


async def tail_log(server_id: int, lines: int = 100, hide_spam: bool = True) -> str:
    wanted = max(1, min(1000, int(lines)))
    body = await _api(
        "GET", f"/api/servers/{server_id}/log", params={"hide_spam": hide_spam}
    )
    body["lines"] = list(body.get("lines") or [])[-wanted:]
    return to_json(body, fallback=str, indent=2).decode()


async def server_overview() -> str:
    servers = await _api("GET", "/api/servers")
    engine = await _api("GET", "/api/engine")
    recent_jobs = await _api("GET", "/api/jobs", params={"limit": 5})
    running = [s["id"] for s in servers if s.get("is_running")]
    return to_json(
        {
            "servers": servers,
            "running_server_id": running[0] if running else None,
            "engine": engine,
            "recent_jobs": recent_jobs,
        },
        fallback=str,
        indent=2,
    ).decode()


def register_composites(mcp: MCPServer) -> None:
    """Register the three composite read-only tools on the given MCP server."""
    mcp.add_tool(
        wait_for_job,
        name="wait_for_job",
        description=(
            "Block until a background job finishes and return its full final "
            "record: state, progress, final result or error, and the log tail. "
            "Pass the job_id that any job-returning tool (engine update, mod "
            "scan, mod-update apply) gave you; polls until the job reaches a "
            "terminal state (succeeded/failed/cancelled). If the job is still "
            "running when timeout_s (default 60) elapses, the current snapshot "
            "is returned with timed_out=true instead of an error — call again "
            "to keep waiting; poll_s tunes the poll interval. A 404 means the "
            "job id is unknown or its row was pruned."
        ),
        structured_output=False,
    )
    mcp.add_tool(
        tail_log,
        name="tail_log",
        description=(
            "Read the tail of a server's console log: the last `lines` entries "
            "(1-1000, default 100), each with its line number, text, detected "
            "severity (debug/info/warning/error) and spam flag. hide_spam=true "
            "(default) drops the configured spam-pattern lines server-side "
            "before slicing. 404 when the server definition is unknown or it "
            "has no console log yet (never started)."
        ),
        structured_output=False,
    )
    mcp.add_tool(
        server_overview,
        name="server_overview",
        description=(
            "One-shot situation report: every stored server definition with "
            "its runtime flags, which server id (if any) is currently running "
            "(the single-server rule means at most one), the engine build "
            "status row, and the 5 most recent background jobs newest first. "
            "Start here to see the manager's overall state before picking a "
            "server or job to act on."
        ),
        structured_output=False,
    )
