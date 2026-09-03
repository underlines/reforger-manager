"""GET    /api/mcp/tokens      -> list MCP tokens (never the hash; revoked_at set = revoked)
POST   /api/mcp/tokens      -> mint a token; the raw ``rfm_...`` value is returned
                               exactly once, only its sha256 hash is stored
DELETE /api/mcp/tokens/{id} -> soft-revoke (sets ``revoked_at``); idempotent
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.security import get_current_user
from ..models import McpToken
from ..schemas.mcp import McpTokenCreate, McpTokenCreatedOut, McpTokenOut

router = APIRouter(prefix="/mcp/tokens", tags=["mcp"])
authed = [Depends(get_current_user)]

_TOKEN_PREFIX = "rfm_"


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _out(row: McpToken) -> McpTokenOut:
    return McpTokenOut.model_validate(row)


@router.get("", response_model=list[McpTokenOut], dependencies=authed)
async def list_tokens(session: AsyncSession = Depends(get_session)) -> list[McpTokenOut]:
    rows = (await session.execute(select(McpToken).order_by(McpToken.id.desc()))).scalars().all()
    return [_out(row) for row in rows]


@router.post(
    "",
    response_model=McpTokenCreatedOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=authed,
)
async def create_token(
    body: McpTokenCreate, session: AsyncSession = Depends(get_session)
) -> McpTokenCreatedOut:
    raw = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = McpToken(
        label=body.label,
        token_hash=_hash_token(raw),
        expires_at=body.expires_at,
    )
    session.add(row)
    await session.commit()
    return McpTokenCreatedOut(token=raw, **_out(row).model_dump())


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=authed)
async def revoke_token(token_id: int, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(McpToken, token_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "token not found")
    if row.revoked_at is None:
        # Idempotent: revoking an already-revoked token is a no-op that keeps
        # the original revocation timestamp.
        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()
