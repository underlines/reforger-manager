"""In-process API adapter: MCP tools call our own REST API, never re-implement it.

One shared ``httpx.AsyncClient`` drives the real FastAPI app through
``ASGITransport`` — the same mechanism the route tests use, so a tool call
exercises the identical code path as the webui (DI sessions, job manager,
409s, single-server rule). ``call()`` attaches the JWT minted by the auth
middleware from the context var; non-2xx responses raise ``ApiError`` for the
tool layer to surface verbatim.
"""

from __future__ import annotations

from typing import Any

import httpx

from .auth import current_jwt


class ApiError(Exception):
    """An API call returned a non-2xx response."""

    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API {status_code}: {detail}")


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Resolved lazily (request time, not import time) so main.py can import
        # this package without an import cycle.
        from ..main import app

        _client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        )
    return _client


async def call(
    method: str,
    path: str,
    *,
    json: Any | None = None,
    params: Any | None = None,
    files: Any | None = None,
    timeout_s: float = 60.0,
) -> Any:
    client = _get_client()
    token = current_jwt()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    response = await client.request(
        method, path, json=json, params=params, files=files, headers=headers, timeout=timeout_s
    )
    if response.is_error:
        try:
            body: Any = response.json()
            detail = body.get("detail", body) if isinstance(body, dict) else body
        except ValueError:
            detail = response.text
        raise ApiError(response.status_code, detail)
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"body": response.text}
