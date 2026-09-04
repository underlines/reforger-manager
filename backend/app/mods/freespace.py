"""Free-space guard shared by every route that enqueues a download.

``Mod.size`` is the Workshop package total and is ``NULL`` for anything
unenriched, so a sum over the affected set must never silently become "0 bytes,
plenty of room" (S11 watch-out). When any size is unknown the guard refuses
rather than guaranteeing a fit it cannot compute.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models import Mod, ServerMod
from .sync import enrich_one
from .workshop import workshop

logger = logging.getLogger("reforger.mods.freespace")


def estimate_download_bytes(mods: list[Mod]) -> tuple[int | None, list[str]]:
    """Project the download size for ``mods``.

    Returns ``(sum of known sizes, unknown guids)``. When ANY affected mod has a
    ``NULL`` ``size`` the projection is ``None`` — meaning "cannot guarantee".
    """
    unknown = [m.guid for m in mods if m.size is None]
    if unknown:
        return None, unknown
    return sum(m.size or 0 for m in mods), []


def check_free_space(mods_dir: Path, projected: int | None) -> None:
    """Raise 409 when ``projected`` bytes cannot be guaranteed to fit.

    ``projected is None`` means at least one affected mod has no recorded size;
    the operator must verify disk space manually, so a 409 is raised instead of
    guessing.
    """
    free = shutil.disk_usage(mods_dir).free
    if projected is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "cannot verify free space: at least one affected mod has no recorded "
            f"size; check disk space on {mods_dir} manually before continuing",
        )
    if projected > free:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"download of {projected} bytes does not fit: only {free} bytes are "
            f"free on {mods_dir}",
        )


async def ensure_sizes(session: AsyncSession, mods: list[Mod]) -> None:
    """Best-effort, one-shot backfill of ``Mod.size`` for never-downloaded rows.

    Used by the free-space guard so a never-downloaded (``is_local``) mod with a
    NULL size can be sized instead of refusing on "no recorded size". A local row
    keeps its NULL size — a downloaded addon with no recorded size is an anomaly
    the guard still refuses rather than guessing. Exactly ONE enrich attempt per
    affected row; a per-row failure is logged and swallowed so the caller falls
    through to its existing 409 path with the size still unknown.
    """
    for mod in mods:
        if mod.size is None and not mod.is_local:
            try:
                await enrich_one(session, mod.guid, client=workshop)
                await session.commit()
            except Exception:  # noqa: BLE001 - one bad row must not block the guard
                logger.exception("failed to backfill size for %s", mod.guid)


async def guard_update_scope(session: AsyncSession, scope: str | int) -> None:
    """Free-space guard for the update-apply scopes (``'all'`` or a server id).

    Mirrors ``mods.updates._targets_for_scope`` so the projected set matches what
    the enqueued apply job will actually download: every local mod for
    ``'all'``, every enabled assigned mod for a server scope. Runs BEFORE the
    job is enqueued so a too-big apply is refused up front.
    """
    if scope == "all":
        mods = (await session.execute(select(Mod).where(Mod.is_local.is_(True)))).scalars().all()
    else:
        mods = (
            await session.execute(
                select(Mod)
                .join(ServerMod, ServerMod.mod_guid == Mod.guid)
                .where(ServerMod.server_id == scope, ServerMod.enabled.is_(True))
            )
        ).scalars().all()
    mods = list(mods)
    await ensure_sizes(session, mods)
    projected, _unknown = estimate_download_bytes(mods)
    check_free_space(settings.mods_dir, projected)