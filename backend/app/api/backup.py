# register in main.py: app.include_router(backup_api.router, prefix=_API)
"""Full backup export / import (S18, PLAN F29-F31).

GET  /api/backup/export
    -> one JSON document: every server definition (config fields + its mod set,
       pins and all three ``*_password`` in CLEARTEXT) and every modpack.
       Excludes: row ids, every runtime column, the generated ``config`` blob,
       ``config_revision``, the ``engine`` row and the disk-derived ``mods``
       library -- all reconstructable.

POST /api/backup/import?dry_run=true|false&on_conflict=skip|replace
    body = an export document. Defaults: dry_run=true, on_conflict=skip.
    - dry_run=true  -> return the PLAN and change nothing.
    - dry_run=false -> apply it and return the same shape with the actions taken.
    Servers and packs are keyed by ``name``. Existing name + skip -> "skip";
    existing name + replace -> overwrite that definition's config fields and mod
    set (via the shared ``_apply_mods`` reconcile helper -- fact #1: a mod that
    survives the replace keeps any pin) / that pack's description + items.
    Never touches a running server: if ``replace`` would replace the definition
    whose name matches the running ``supervisor.active_server_id`` row, its
    action is forced to "skip" (in both the plan and the real run) and noted.
    A ``mod_guid`` absent from the local ``mods`` table is still imported (the
    library is disk-derived) -- ``mod_name`` comes from the backup.
    A malformed document (missing/!array ``servers``/``modpacks``) -> 422.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.db import get_session
from ..core.security import get_current_user
from ..models import Modpack, ModpackItem, Server, ServerMod
from ..schemas.backup import (
    SERVER_CONFIG_FIELDS,
    BackupDocument,
    BackupItemPlan,
    BackupMod,
    BackupModpack,
    BackupModpackItem,
    BackupPlan,
    BackupServer,
)
from ..schemas.server import ServerModIn
from ..servers.supervisor import supervisor
from .servers import _apply_mods

router = APIRouter(prefix="/backup", tags=["backup"])
authed = [Depends(get_current_user)]


# --------------------------------------------------------------------- export


@router.get("/export", response_model=BackupDocument, dependencies=authed)
async def export_backup(session: AsyncSession = Depends(get_session)) -> BackupDocument:
    servers = (
        await session.execute(
            select(Server).options(selectinload(Server.mods)).order_by(Server.id)
        )
    ).scalars().all()
    modpacks = (
        await session.execute(
            select(Modpack).options(selectinload(Modpack.items)).order_by(Modpack.id)
        )
    ).scalars().all()

    return BackupDocument(
        version=1,
        exported_at=datetime.now(timezone.utc),
        servers=[
            BackupServer(
                **{field: getattr(srv, field) for field in SERVER_CONFIG_FIELDS},
                mods=[
                    BackupMod.model_validate(sm)
                    for sm in sorted(srv.mods, key=lambda sm: sm.load_order)
                ],
            )
            for srv in servers
        ],
        modpacks=[
            BackupModpack(
                name=pack.name,
                description=pack.description,
                items=[
                    BackupModpackItem(mod_guid=it.mod_guid, load_order=it.load_order)
                    for it in sorted(pack.items, key=lambda it: it.load_order)
                ],
            )
            for pack in modpacks
        ],
    )


# --------------------------------------------------------------------- import


def _mods_payload(mods: list[BackupMod]) -> list[ServerModIn]:
    """Backup mod rows -> ``_apply_mods`` input (``ServerModIn``-shaped).

    ``pinned_at`` has no ``ServerModIn`` field; it is restored directly after
    ``_apply_mods`` runs (see ``_restore_pinned_at``).
    """
    return [
        ServerModIn(
            mod_guid=m.mod_guid,
            mod_name=m.mod_name,
            load_order=m.load_order,
            enabled=m.enabled,
            pinned_version=m.pinned_version,
            pinned_at_build=m.pinned_at_build,
            pinned_reason=m.pinned_reason,
        )
        for m in mods
    ]


def _restore_pinned_at(server: Server, mods: list[BackupMod]) -> None:
    """Carry the backup's ``pinned_at`` timestamps onto the freshly applied rows.

    ``_apply_mods`` clears ``pinned_at`` whenever it (re)pins from a payload that
    carries ``pinned_version`` -- which every exported pin does -- so a faithful
    restore has to put the original timestamp back.
    """
    by_guid = {sm.mod_guid: sm for sm in server.mods}
    for m in mods:
        row = by_guid.get(m.mod_guid)
        if row is not None and m.pinned_at is not None:
            row.pinned_at = m.pinned_at


def _server_action(
    entry: BackupServer,
    existing: dict[str, Server],
    on_conflict: str,
    running_name: str | None,
) -> tuple[str, str | None]:
    if entry.name not in existing:
        return "create", None
    if on_conflict != "replace":
        return "skip", None
    if running_name is not None and entry.name == running_name:
        return "skip", "server is running - definition left untouched"
    return "replace", None


def _pack_action(entry: BackupModpack, existing: dict[str, Modpack], on_conflict: str) -> str:
    if entry.name not in existing:
        return "create"
    return "replace" if on_conflict == "replace" else "skip"


@router.post("/import", response_model=BackupPlan, dependencies=authed)
async def import_backup(
    body: BackupDocument,
    dry_run: bool = Query(default=True),
    on_conflict: Literal["skip", "replace"] = Query(default="skip"),
    session: AsyncSession = Depends(get_session),
) -> BackupPlan:
    running_name: str | None = None
    if supervisor.is_running() and supervisor.active_server_id is not None:
        running_name = (
            await session.execute(
                select(Server.name).where(Server.id == supervisor.active_server_id)
            )
        ).scalar_one_or_none()

    existing_servers = {
        srv.name: srv
        for srv in (
            await session.execute(
                select(Server).options(selectinload(Server.mods))
            )
        ).scalars().all()
    }
    existing_packs = {
        pack.name: pack
        for pack in (
            await session.execute(
                select(Modpack).options(selectinload(Modpack.items))
            )
        ).scalars().all()
    }

    server_plan: list[BackupItemPlan] = []
    for entry in body.servers:
        action, note = _server_action(entry, existing_servers, on_conflict, running_name)
        server_plan.append(BackupItemPlan(name=entry.name, action=action, note=note))
        if dry_run:
            continue
        if action == "create":
            srv = Server(**{field: getattr(entry, field) for field in SERVER_CONFIG_FIELDS})
            _apply_mods(srv, _mods_payload(entry.mods))
            _restore_pinned_at(srv, entry.mods)
            session.add(srv)
        elif action == "replace":
            srv = existing_servers[entry.name]
            for field in SERVER_CONFIG_FIELDS:
                setattr(srv, field, getattr(entry, field))
            _apply_mods(srv, _mods_payload(entry.mods))
            _restore_pinned_at(srv, entry.mods)

    modpack_plan: list[BackupItemPlan] = []
    for entry in body.modpacks:
        action = _pack_action(entry, existing_packs, on_conflict)
        modpack_plan.append(BackupItemPlan(name=entry.name, action=action))
        if dry_run:
            continue
        if action == "create":
            pack = Modpack(name=entry.name, description=entry.description)
            pack.items.extend(
                ModpackItem(mod_guid=it.mod_guid, load_order=it.load_order)
                for it in entry.items
            )
            session.add(pack)
        elif action == "replace":
            pack = existing_packs[entry.name]
            pack.description = entry.description
            pack.items.clear()
            await session.flush()  # emit item DELETEs before re-inserting (uq_modpack_item)
            pack.items.extend(
                ModpackItem(mod_guid=it.mod_guid, load_order=it.load_order)
                for it in entry.items
            )

    if dry_run:
        await session.rollback()  # belt and braces: a dry run persists nothing
    else:
        await session.commit()

    return BackupPlan(
        dry_run=dry_run,
        on_conflict=on_conflict,
        servers=server_plan,
        modpacks=modpack_plan,
    )
