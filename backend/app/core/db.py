"""Async SQLAlchemy engine, session factory and declarative Base."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import settings

# JSONB on Postgres, plain JSON elsewhere (sqlite for local checks).
JSONVariant = JSON().with_variant(JSONB, "postgresql")

_engine_kwargs: dict = {"echo": False, "future": True}
if settings.database_url.startswith("postgresql"):
    _engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(settings.database_url, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    async with SessionLocal() as session:
        yield session
