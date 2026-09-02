"""GET /api/health — unauthenticated liveness probe (compose healthcheck hits it)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
