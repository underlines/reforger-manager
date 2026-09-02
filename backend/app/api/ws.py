"""Shared WebSocket auth helper.

Browsers cannot set an Authorization header on a WebSocket, so the JWT is passed
as ``?token=<jwt>`` (or, failing that, the ``Authorization: Bearer`` header for
non-browser clients). Closes with 1008 on failure.
"""

from __future__ import annotations

from jose import JWTError
from sqlalchemy import select
from starlette.websockets import WebSocket

from ..core.db import SessionLocal
from ..core.security import decode_token
from ..models import User


async def ws_authenticate(websocket: WebSocket, token: str | None) -> bool:
    if not token:
        header = websocket.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]
    if not token:
        await websocket.close(code=1008)
        return False
    try:
        payload = decode_token(token)
        username = payload.get("sub")
    except JWTError:
        await websocket.close(code=1008)
        return False
    if not username:
        await websocket.close(code=1008)
        return False
    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
    if user is None or not user.is_active:
        await websocket.close(code=1008)
        return False
    return True
