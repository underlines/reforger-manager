"""Mod library — disk-scanned, Workshop-enriched.

GET  /api/mods                      list (filters: ?local ?q ?update ?state)
GET  /api/mods/{guid}               full detail (+ versions cache, dep tree, used_by)
GET  /api/mods/{guid}/dependencies  resolved dependency tree only
POST /api/mods/scan                 enqueue the `mod_sync` job
POST /api/mods/add                  add by Workshop URL or bare 16-hex id
GET  /api/mods/search?q=            proxy to the Workshop search
"""

from __future__ import annotations

import re
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.config import settings
from ..core.db import get_session
from ..core.jobs import job_manager
from ..core.security import get_current_user
from ..models import ENGINE_SINGLETON_ID, Engine, Mod, Server, ServerMod
from ..mods.downloader import MOD_DOWNLOAD_JOB_KIND
from ..mods.freespace import check_free_space, estimate_download_bytes, guard_update_scope
from ..mods.pinning import CurrentEngineBuildMissing, PinRecordNotFound, pin_mod, unpin_mod
from ..mods.resolve import resolve_dependencies
from ..mods.scanner import addons_root, resolve_addon_dir
from ..mods.sync import enrich_one
from ..mods.updates import (
    MOD_UPDATE_APPLY_JOB_KIND,
    MOD_UPDATE_CHECK_JOB_KIND,
)
from ..mods.verify import VERIFY_REPAIR_JOB_KIND
from ..mods.workshop import ModNotFound, WorkshopError, workshop
from ..schemas.job import JobEnqueuedOut
from ..schemas.mod import (
    ModAddIn,
    ModDetailOut,
    ModDownloadIn,
    ModOut,
    ModPinIn,
    ModSearchResult,
    ResolvedTreeOut,
    ModVerifyIn,
)
from ..servers.supervisor import supervisor
from .storage import orphan_reference_sets

router = APIRouter(prefix="/mods", tags=["mods"], dependencies=[Depends(get_current_user)])

JOB_KIND_MOD_SYNC = "mod_sync"

_GUID_RE = re.compile(r"[0-9A-Fa-f]{16}")


async def _engine_build(session: AsyncSession) -> str | None:
    row = await session.get(Engine, ENGINE_SINGLETON_ID)
    return row.installed_build if row is not None else None


def _has_update(mod: Mod) -> bool:
    return bool(
        mod.latest_version
        and mod.installed_version
        and mod.latest_version != mod.installed_version
    )


def _stale_pin(mod: Mod, engine_build: str | None) -> bool:
    # Placeholder — full pin/stale logic is a later phase. Exposed as a plain
    # build-mismatch check so the column is usable now.
    return bool(mod.pinned_at_build and engine_build and mod.pinned_at_build != engine_build)


def _to_out(mod: Mod, engine_build: str | None) -> ModOut:
    out = ModOut.model_validate(mod)
    out.has_update = _has_update(mod)
    out.stale_pin = _stale_pin(mod, engine_build)
    return out


# --------------------------------------------------------------------- list
@router.get("", response_model=list[ModOut])
async def list_mods(
    local: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    update: bool | None = Query(default=None),
    state: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[ModOut]:
    stmt = select(Mod).order_by(Mod.name.is_(None), Mod.name, Mod.guid)
    if local is not None:
        stmt = stmt.where(Mod.is_local.is_(local))
    if q:
        stmt = stmt.where(Mod.name.ilike(f"%{q}%"))
    if update:
        stmt = stmt.where(
            Mod.latest_version.isnot(None),
            Mod.installed_version.isnot(None),
            Mod.latest_version != Mod.installed_version,
        )
    if state:
        stmt = stmt.where(Mod.api_state == state)

    rows = (await session.execute(stmt)).scalars().all()
    engine_build = await _engine_build(session)
    return [_to_out(m, engine_build) for m in rows]


# ------------------------------------------------------------------ search
# Declared before "/{guid}" so it is not shadowed by the path param.
@router.get("/search", response_model=list[ModSearchResult])
async def search_mods(
    q: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=80),
) -> list[ModSearchResult]:
    try:
        results = await workshop.search(q, limit=limit)
    except WorkshopError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Workshop search failed: {exc}"
        )
    return [
        ModSearchResult(
            id=str(r.get("id")),
            name=r.get("name"),
            summary=r.get("summary"),
            latest_version=r.get("version"),
            workshop_url=r.get("workshop_url"),
        )
        for r in results
        if r.get("id")
    ]


# ------------------------------------------------------------- detail / deps
# These static actions must precede /{guid} to avoid FastAPI path shadowing.
@router.post("/updates/check", response_model=JobEnqueuedOut, status_code=status.HTTP_202_ACCEPTED)
async def check_all_updates() -> JobEnqueuedOut:
    job_id = await job_manager.enqueue(MOD_UPDATE_CHECK_JOB_KIND, params={"scope": "all"})
    return JobEnqueuedOut(job_id=job_id, kind=MOD_UPDATE_CHECK_JOB_KIND)


@router.post("/updates/apply", response_model=JobEnqueuedOut, status_code=status.HTTP_202_ACCEPTED)
async def apply_all_updates(session: AsyncSession = Depends(get_session)) -> JobEnqueuedOut:
    await guard_update_scope(session, "all")
    job_id = await job_manager.enqueue(MOD_UPDATE_APPLY_JOB_KIND, params={"scope": "all"})
    return JobEnqueuedOut(job_id=job_id, kind=MOD_UPDATE_APPLY_JOB_KIND)


@router.post("/verify", response_model=JobEnqueuedOut, status_code=status.HTTP_202_ACCEPTED)
async def verify_mods(body: ModVerifyIn | None = None) -> JobEnqueuedOut:
    job_id = await job_manager.enqueue(
        VERIFY_REPAIR_JOB_KIND, params={"guids": body.guids if body else None}
    )
    return JobEnqueuedOut(job_id=job_id, kind=VERIFY_REPAIR_JOB_KIND)


@router.post("/{guid}/pin", response_model=ModOut)
async def pin_library_mod(
    guid: str, body: ModPinIn, session: AsyncSession = Depends(get_session)
) -> ModOut:
    try:
        mod = await pin_mod(session, guid, body.version, body.reason)
    except PinRecordNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except CurrentEngineBuildMissing as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await session.commit()
    return _to_out(mod, await _engine_build(session))


@router.delete("/{guid}/pin", response_model=ModOut)
async def unpin_library_mod(
    guid: str, session: AsyncSession = Depends(get_session)
) -> ModOut:
    try:
        mod = await unpin_mod(session, guid)
    except PinRecordNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    await session.commit()
    return _to_out(mod, await _engine_build(session))


@router.post("/{guid}/download", response_model=JobEnqueuedOut, status_code=status.HTTP_202_ACCEPTED)
async def download_mod(
    guid: str, body: ModDownloadIn | None = None, session: AsyncSession = Depends(get_session)
) -> JobEnqueuedOut:
    """Force a (re)download of one mod. Enqueues the registered ``mod_download``
    job (fact #6) after the free-space guard (S11)."""
    guid = guid.upper()
    mod = await session.get(Mod, guid)
    if mod is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "mod not found")
    projected, _unknown = estimate_download_bytes([mod])
    check_free_space(settings.mods_dir, projected)
    versions = {guid: body.version} if body and body.version else {}
    job_id = await job_manager.enqueue(
        MOD_DOWNLOAD_JOB_KIND, params={"guids": [guid], "versions": versions}
    )
    return JobEnqueuedOut(job_id=job_id, kind=MOD_DOWNLOAD_JOB_KIND)


@router.delete("/{guid}/local", response_model=ModOut)
async def delete_local_mod(
    guid: str, session: AsyncSession = Depends(get_session)
) -> ModOut:
    """Delete a mod's on-disk addon dir and clear ``is_local`` (S12).

    The orphan condition is re-checked server-side because the client's list may
    be stale; deletion is refused while any server runs, and the addon path must
    resolve to a real directory inside ``scanner.addons_root()`` — a computed
    path outside it is never removed.
    """
    guid = guid.upper()
    if supervisor.is_running():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "stop the running server before removing mod files from disk",
        )
    mod = await session.get(Mod, guid)
    if mod is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "mod not found")
    if not mod.is_local:
        raise HTTPException(status.HTTP_409_CONFLICT, "mod has no on-disk files to remove")

    directly_referenced, closure_owners = await orphan_reference_sets(session)
    if guid in directly_referenced or guid in closure_owners:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"mod {guid} is still referenced by a server definition, modpack, "
            "or resolved dependency; refusing to delete",
        )

    root = addons_root().resolve()
    addon_dir = resolve_addon_dir(guid)
    if addon_dir is None or not addon_dir.resolve().is_relative_to(root):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"mod {guid} does not resolve to an addon directory inside {root}",
        )

    shutil.rmtree(addon_dir)
    mod.is_local = False
    await session.commit()
    return _to_out(mod, await _engine_build(session))


@router.delete("/{guid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library_mod(
    guid: str, session: AsyncSession = Depends(get_session)
) -> None:
    """Delete a mod's library row entirely (row + on-disk files, if any).

    Unlike ``DELETE /{guid}/local`` (which only clears the disk cache and keeps
    the catalogue entry), this removes the ``mods`` row so a stale, undownloaded,
    unreferenced entry can be cleared from the library. Guards mirror the
    disk-delete route: refused while a server runs if the mod still has files,
    and refused when the mod is still directly referenced or held alive by the
    resolved dependency closure of any assigned/packed mod.
    """
    guid = guid.upper()
    mod = await session.get(Mod, guid)
    if mod is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "mod not found")
    if mod.is_local and supervisor.is_running():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "stop the running server before removing a local mod from the library",
        )

    directly_referenced, closure_owners = await orphan_reference_sets(session)
    if guid in directly_referenced or guid in closure_owners:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"mod {guid} is still referenced by a server definition, modpack, "
            "or resolved dependency; refusing to delete",
        )

    if mod.is_local:
        root = addons_root().resolve()
        addon_dir = resolve_addon_dir(guid)
        if addon_dir is not None and addon_dir.resolve().is_relative_to(root):
            shutil.rmtree(addon_dir, ignore_errors=True)

    await session.delete(mod)
    await session.commit()


@router.get("/{guid}", response_model=ModDetailOut)
async def get_mod_detail(
    guid: str, session: AsyncSession = Depends(get_session)
) -> ModDetailOut:
    guid = guid.upper()
    mod = (
        await session.execute(
            select(Mod)
            .where(Mod.guid == guid)
            .options(selectinload(Mod.scenarios), selectinload(Mod.dependencies))
        )
    ).scalar_one_or_none()
    if mod is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "mod not found")

    engine_build = await _engine_build(session)
    base = _to_out(mod, engine_build)

    versions: list[dict] = []
    try:
        versions = await workshop.get_versions(guid)
    except ModNotFound:
        versions = []
    except WorkshopError:
        versions = []

    tree = await resolve_dependencies(session, [guid])

    used_by = list(
        (
            await session.execute(
                select(Server.name)
                .join(ServerMod, ServerMod.server_id == Server.id)
                .where(ServerMod.mod_guid == guid)
                .order_by(Server.name)
            )
        ).scalars().all()
    )

    return ModDetailOut(
        **base.model_dump(),
        scenarios=list(mod.scenarios),
        dependencies=list(mod.dependencies),
        dependency_tree=ResolvedTreeOut(**tree.as_dict()),
        versions=versions,
        used_by=used_by,
    )


@router.get("/{guid}/dependencies", response_model=ResolvedTreeOut)
async def get_mod_dependencies(
    guid: str, session: AsyncSession = Depends(get_session)
) -> ResolvedTreeOut:
    guid = guid.upper()
    tree = await resolve_dependencies(session, [guid])
    return ResolvedTreeOut(**tree.as_dict())


# --------------------------------------------------------------- scan / add
@router.post("/scan", response_model=JobEnqueuedOut, status_code=status.HTTP_202_ACCEPTED)
async def scan_mods() -> JobEnqueuedOut:
    job_id = await job_manager.enqueue(JOB_KIND_MOD_SYNC)
    return JobEnqueuedOut(job_id=job_id, kind=JOB_KIND_MOD_SYNC)


@router.post("/add", response_model=ModOut, status_code=status.HTTP_201_CREATED)
async def add_mod(
    body: ModAddIn, session: AsyncSession = Depends(get_session)
) -> ModOut:
    m = _GUID_RE.search(body.url_or_id or "")
    if not m:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "no 16-hex Workshop GUID found in the input",
        )
    guid = m.group(0).upper()

    try:
        await workshop.get_mod(guid)
    except ModNotFound:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"mod {guid} is not resolvable on the Workshop "
            "(deleted, blocked, or private)",
        )
    except WorkshopError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Workshop API unavailable: {exc}"
        )

    row = await enrich_one(session, guid, client=workshop)
    await session.commit()
    await session.refresh(row)

    engine_build = await _engine_build(session)
    return _to_out(row, engine_build)
