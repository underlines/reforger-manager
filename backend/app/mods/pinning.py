"""Library and per-server mod-version pinning.

``is_stale(pin, engine)`` accepts ORM objects (``Mod`` or ``ServerMod`` and
``Engine``), while ``is_stale(pinned_at_build="...", installed_build="...")``
is convenient for callers that already have just the two build values.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.engine import ENGINE_SINGLETON_ID, Engine
from ..models.mod import Mod
from ..models.server import ServerMod


class PinningError(Exception):
    """Base exception for pinning operations."""


class PinRecordNotFound(PinningError):
    """The requested mod or server-mod assignment does not exist."""


class CurrentEngineBuildMissing(PinningError):
    """A pin cannot be recorded until the installed engine build is known."""


def _normalise_guid(guid: str) -> str:
    return guid.upper()


async def _current_engine_build(session: AsyncSession) -> str:
    engine = await session.get(Engine, ENGINE_SINGLETON_ID)
    if engine is None or not engine.installed_build:
        raise CurrentEngineBuildMissing(
            "Cannot pin a mod because the current installed engine build is missing."
        )
    return engine.installed_build


def _apply_pin(
    pin: Mod | ServerMod, version: str, reason: str | None, installed_build: str
) -> None:
    if not version:
        raise ValueError("A pinned mod version is required.")
    pin.pinned_version = version
    pin.pinned_at_build = installed_build
    pin.pinned_reason = reason
    pin.pinned_at = datetime.now(UTC)


async def pin_mod(
    session: AsyncSession, guid: str, version: str, reason: str | None = None
) -> Mod:
    """Pin a library mod version against the current installed engine build."""
    normalized_guid = _normalise_guid(guid)
    mod = await session.get(Mod, normalized_guid)
    if mod is None:
        raise PinRecordNotFound(f"Mod {normalized_guid} was not found.")

    _apply_pin(mod, version, reason, await _current_engine_build(session))
    return mod


async def unpin_mod(session: AsyncSession, guid: str) -> Mod:
    """Clear a library-level mod pin."""
    normalized_guid = _normalise_guid(guid)
    mod = await session.get(Mod, normalized_guid)
    if mod is None:
        raise PinRecordNotFound(f"Mod {normalized_guid} was not found.")

    _clear_pin(mod)
    return mod


async def pin_server_mod(
    session: AsyncSession,
    server_id: int,
    guid: str,
    version: str,
    reason: str | None = None,
) -> ServerMod:
    """Pin one mod assignment for a server against the current engine build."""
    normalized_guid = _normalise_guid(guid)
    server_mod = await session.scalar(
        select(ServerMod).where(
            ServerMod.server_id == server_id,
            ServerMod.mod_guid == normalized_guid,
        )
    )
    if server_mod is None:
        raise PinRecordNotFound(
            f"Mod {normalized_guid} is not assigned to server {server_id}."
        )

    _apply_pin(server_mod, version, reason, await _current_engine_build(session))
    return server_mod


async def unpin_server_mod(session: AsyncSession, server_id: int, guid: str) -> ServerMod:
    """Clear a per-server mod pin."""
    normalized_guid = _normalise_guid(guid)
    server_mod = await session.scalar(
        select(ServerMod).where(
            ServerMod.server_id == server_id,
            ServerMod.mod_guid == normalized_guid,
        )
    )
    if server_mod is None:
        raise PinRecordNotFound(
            f"Mod {normalized_guid} is not assigned to server {server_id}."
        )

    _clear_pin(server_mod)
    return server_mod


def _clear_pin(pin: Mod | ServerMod) -> None:
    pin.pinned_version = None
    pin.pinned_at_build = None
    pin.pinned_reason = None
    pin.pinned_at = None


def is_stale(
    pin: Mod | ServerMod | None = None,
    engine: Engine | None = None,
    *,
    pinned_at_build: str | None = None,
    installed_build: str | None = None,
) -> bool:
    """Return whether a pin was created against a different installed build.

    Use either ``is_stale(pin, engine)`` with ORM objects, or pass the values
    directly as ``is_stale(pinned_at_build=..., installed_build=...)``. A pin
    without a recorded build, or an unknown installed build, is not stale.
    """
    if pinned_at_build is None and pin is not None:
        pinned_at_build = pin.pinned_at_build
    if installed_build is None and engine is not None:
        installed_build = engine.installed_build
    return bool(pinned_at_build and installed_build and pinned_at_build != installed_build)
