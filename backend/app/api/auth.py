"""POST /api/auth/login     -> JWT  (unauthenticated)
GET  /api/auth/me        -> current user
POST /api/auth/password  -> self-service password change (session stays valid)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_session
from ..core.security import create_access_token, get_current_user, hash_password, verify_password
from ..models import User
from ..schemas.auth import LoginRequest, PasswordChangeRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    user = (
        await session.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    user.last_login_at = datetime.now(timezone.utc)
    await session.commit()

    token = create_access_token(user.username, extra={"uid": user.id})
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_hours * 3600,
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/password")
async def change_password(
    body: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    # Re-hash and persist; the bootstrap seed is empty-table-only, so the change
    # survives restarts (fact #5). The JWT stays valid — no rotation.
    user.password_hash = hash_password(body.new_password)
    await session.commit()
    return {"status": "password_changed"}
