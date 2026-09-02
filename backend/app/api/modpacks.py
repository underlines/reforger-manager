"""Modpack CRUD: named, reusable, load-ordered mod lists.

GET    /api/modpacks        list
POST   /api/modpacks        create
GET    /api/modpacks/{id}   read
PATCH  /api/modpacks/{id}   update (PATCH semantics; `items` replaces the set)
DELETE /api/modpacks/{id}   delete

Duplicate GUIDs inside one `items[]` payload are rejected at the schema layer
(422); unique-constraint violations (modpacks.name, uq_modpack_item) surface
as 409 from the IntegrityError catch around flush/commit, never as a 500.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.db import get_session
from ..core.security import get_current_user
from ..models import Mod, Modpack, ModpackItem, Server, ServerMod
from ..schemas.modpack import (
    DroppedPin,
    ModpackApplyIn,
    ModpackApplyOut,
    ModpackCreate,
    ModpackFromServerIn,
    ModpackFromServerOut,
    ModpackItemIn,
    ModpackItemOut,
    ModpackOut,
    ModpackTransfer,
    ModpackUpdate,
)
from ..schemas.server import ServerModIn
from .servers import _apply_mods
from .servers import _load as _load_server

router = APIRouter(prefix="/modpacks", tags=["modpacks"])
authed = [Depends(get_current_user)]


async def _load(session: AsyncSession, pack_id: int) -> Modpack:
    # populate_existing: after a same-session create/update the pack is already in
    # the identity map; without this the reload would keep the in-memory item
    # order/timestamps instead of the committed DB state.
    pack = (
        await session.execute(
            select(Modpack)
            .where(Modpack.id == pack_id)
            .options(selectinload(Modpack.items))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if pack is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "modpack not found")
    return pack


async def _mod_names(session: AsyncSession, guids: list[str]) -> dict[str, str | None]:
    unique = sorted(set(guids))
    if not unique:
        return {}
    rows = (await session.execute(select(Mod.guid, Mod.name).where(Mod.guid.in_(unique)))).all()
    return {guid: name for guid, name in rows}


def _to_out(pack: Modpack, names: dict[str, str | None]) -> ModpackOut:
    return ModpackOut(
        id=pack.id,
        name=pack.name,
        description=pack.description,
        items=[
            ModpackItemOut(
                mod_guid=item.mod_guid,
                load_order=item.load_order,
                mod_name=names.get(item.mod_guid),
            )
            for item in pack.items
        ],
        created_at=pack.created_at,
        updated_at=pack.updated_at,
    )


def _integrity_message(exc: IntegrityError) -> str:
    text = str(getattr(exc, "orig", None) or exc)
    if "modpack_items" in text or "mod_guid" in text or "uq_modpack_item" in text:
        return "duplicate mod_guid in the pack's items"
    return "a modpack with that name already exists"


@router.get("", response_model=list[ModpackOut], dependencies=authed)
async def list_modpacks(session: AsyncSession = Depends(get_session)) -> list[ModpackOut]:
    rows = (
        await session.execute(
            select(Modpack).options(selectinload(Modpack.items)).order_by(Modpack.id)
        )
    ).scalars().all()
    names = await _mod_names(session, [i.mod_guid for pack in rows for i in pack.items])
    return [_to_out(pack, names) for pack in rows]


@router.post("", response_model=ModpackOut, status_code=status.HTTP_201_CREATED, dependencies=authed)
async def create_modpack(
    body: ModpackCreate, session: AsyncSession = Depends(get_session)
) -> ModpackOut:
    pack = Modpack(name=body.name, description=body.description)
    pack.items.extend(
        ModpackItem(mod_guid=item.mod_guid, load_order=item.load_order) for item in body.items
    )
    session.add(pack)
    try:
        await session.flush()
        pack_id = pack.id
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _integrity_message(exc))
    pack = await _load(session, pack_id)
    return _to_out(pack, await _mod_names(session, [item.mod_guid for item in pack.items]))


@router.get("/{pack_id}", response_model=ModpackOut, dependencies=authed)
async def get_modpack(pack_id: int, session: AsyncSession = Depends(get_session)) -> ModpackOut:
    pack = await _load(session, pack_id)
    return _to_out(pack, await _mod_names(session, [item.mod_guid for item in pack.items]))


@router.patch("/{pack_id}", response_model=ModpackOut, dependencies=authed)
async def update_modpack(
    pack_id: int, body: ModpackUpdate, session: AsyncSession = Depends(get_session)
) -> ModpackOut:
    pack = await _load(session, pack_id)
    patch = body.model_dump(exclude_unset=True, exclude={"items"})
    for key, value in patch.items():
        setattr(pack, key, value)
    if body.items is not None:
        pack.items.clear()
    try:
        await session.flush()  # emit the item DELETEs (and name UPDATE) before re-inserting
        if body.items is not None:
            pack.items.extend(
                ModpackItem(mod_guid=item.mod_guid, load_order=item.load_order)
                for item in body.items
            )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _integrity_message(exc))
    pack = await _load(session, pack_id)
    return _to_out(pack, await _mod_names(session, [item.mod_guid for item in pack.items]))


@router.delete("/{pack_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=authed)
async def delete_modpack(pack_id: int, session: AsyncSession = Depends(get_session)) -> None:
    pack = await _load(session, pack_id)
    await session.delete(pack)
    await session.commit()


# --------------------------------------------------------------- apply / transfer


@router.post(
    "/{pack_id}/apply/{server_id}",
    response_model=ModpackApplyOut,
    dependencies=authed,
)
async def apply_modpack(
    pack_id: int,
    server_id: int,
    body: ModpackApplyIn,
    session: AsyncSession = Depends(get_session),
) -> ModpackApplyOut:
    """Write a pack's mod list into a server definition's ``server_mods``.

    ``replace`` feeds the pack's full, load-ordered item list through the shared
    ``_apply_mods`` reconcile helper (the same destructive-replace path as
    ``PATCH /api/servers/{id}``): a GUID that survives keeps its row, so a mod
    still in the pack keeps any ``pinned_*`` it had. A pinned mod the pack no
    longer contains loses its row and its pin -- every such GUID is listed in
    ``dropped_pins``.

    ``append`` leaves every currently-assigned mod exactly as it is: an
    already-assigned GUID keeps its current ``load_order`` and its pin and is
    **NOT** moved to the pack's position. Only pack GUIDs not already assigned
    are added, numbered from ``max(existing load_order) + 1`` upward in the
    pack's own order. ``dropped_pins`` is therefore always empty for ``append``.

    404 if the pack or server is missing; 409 while the target server runs.
    """
    pack = await _load(session, pack_id)
    server = await _load_server(session, server_id)
    if server.is_running:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "stop the server before applying a modpack"
        )

    pack_items = sorted(pack.items, key=lambda it: it.load_order)
    names = await _mod_names(session, [it.mod_guid for it in pack_items])
    pack_guids = [it.mod_guid for it in pack_items]

    # Snapshot pins that exist before the apply, keyed by GUID, so we can report
    # the ones that no longer have a row afterwards.
    pins_before = {
        sm.mod_guid: sm.mod_name
        for sm in server.mods
        if sm.pinned_version is not None
    }

    if body.mode == "replace":
        incoming = [
            ServerModIn(
                mod_guid=it.mod_guid,
                mod_name=names.get(it.mod_guid),
                load_order=idx,
                enabled=True,
            )
            for idx, it in enumerate(pack_items)
        ]
        _apply_mods(server, incoming)
    else:  # append -- pure add, existing rows (and their pins) untouched
        assigned = {sm.mod_guid for sm in server.mods}
        next_order = max((sm.load_order for sm in server.mods), default=-1) + 1
        for it in pack_items:
            if it.mod_guid in assigned:
                continue
            server.mods.append(
                ServerMod(
                    mod_guid=it.mod_guid,
                    mod_name=names.get(it.mod_guid),
                    load_order=next_order,
                    enabled=True,
                )
            )
            next_order += 1

    await session.flush()
    post_guids = {sm.mod_guid for sm in server.mods}
    dropped_pins = [
        DroppedPin(mod_guid=guid, mod_name=name)
        for guid, name in pins_before.items()
        if guid not in post_guids
    ]
    applied = sum(1 for guid in set(pack_guids) if guid in post_guids)
    await session.commit()
    return ModpackApplyOut(applied=applied, mode=body.mode, dropped_pins=dropped_pins)


@router.post(
    "/from-server/{server_id}",
    response_model=ModpackFromServerOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=authed,
)
async def modpack_from_server(
    server_id: int,
    body: ModpackFromServerIn,
    session: AsyncSession = Depends(get_session),
) -> ModpackFromServerOut:
    """Snapshot a server definition's current mod set + order into a NEW pack.

    Pins are not carried into the pack; when the source had any, ``pins_note``
    names them. Name collision on ``modpacks.name`` -> 409.
    """
    server = await _load_server(session, server_id)
    ordered = sorted(server.mods, key=lambda sm: sm.load_order)
    pinned_labels = [
        f"{sm.mod_name or sm.mod_guid} ({sm.pinned_version})"
        for sm in ordered
        if sm.pinned_version is not None
    ]

    pack = Modpack(name=body.name, description=body.description)
    pack.items.extend(
        ModpackItem(mod_guid=sm.mod_guid, load_order=sm.load_order) for sm in ordered
    )
    session.add(pack)
    try:
        await session.flush()
        pack_id = pack.id
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _integrity_message(exc))

    pins_note = None
    if pinned_labels:
        pins_note = (
            "Pins were not carried into the pack (a pack is a mod list, not a "
            f"version lock). Dropped pins: {', '.join(pinned_labels)}."
        )
    pack = await _load(session, pack_id)
    out = _to_out(pack, await _mod_names(session, [it.mod_guid for it in pack.items]))
    return ModpackFromServerOut(**out.model_dump(), pins_note=pins_note)


@router.get("/{pack_id}/export", response_model=ModpackTransfer, dependencies=authed)
async def export_modpack(
    pack_id: int, session: AsyncSession = Depends(get_session)
) -> ModpackTransfer:
    """Portable JSON: ``{name, description, items: [{mod_guid, load_order}]}``.

    No ids or timestamps. 404 if the pack is missing.
    """
    pack = await _load(session, pack_id)
    return ModpackTransfer(
        name=pack.name,
        description=pack.description,
        items=[
            ModpackItemIn(mod_guid=it.mod_guid, load_order=it.load_order)
            for it in sorted(pack.items, key=lambda it: it.load_order)
        ],
    )


async def _unique_import_name(session: AsyncSession, base: str) -> str:
    """`base (imported)`, then `base (imported 2)`, ... until unused."""
    candidate = f"{base} (imported)"
    counter = 2
    while (
        await session.execute(select(Modpack.id).where(Modpack.name == candidate))
    ).first() is not None:
        candidate = f"{base} (imported {counter})"
        counter += 1
    return candidate


@router.post("/import", response_model=ModpackOut, dependencies=authed)
async def import_modpack(
    body: ModpackTransfer,
    on_conflict: Literal["rename", "replace", "error"] = Query(default="rename"),
    session: AsyncSession = Depends(get_session),
) -> ModpackOut:
    """Create a pack from an exported document.

    ``on_conflict`` (default ``rename``) governs a ``modpacks.name`` collision:
    ``rename`` appends `` (imported)`` (then `` (imported 2)`` ...) until unique;
    ``replace`` overwrites the existing pack's description + items;
    ``error`` -> 409. A duplicate ``mod_guid`` inside ``items`` -> 422 (schema).
    """
    existing = (
        await session.execute(
            select(Modpack)
            .where(Modpack.name == body.name)
            .options(selectinload(Modpack.items))
        )
    ).scalar_one_or_none()

    if existing is not None and on_conflict == "error":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a modpack with that name already exists"
        )

    if existing is not None and on_conflict == "replace":
        existing.description = body.description
        existing.items.clear()
        try:
            await session.flush()  # emit item DELETEs before re-inserting
            existing.items.extend(
                ModpackItem(mod_guid=item.mod_guid, load_order=item.load_order)
                for item in body.items
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, _integrity_message(exc))
        pack = await _load(session, existing.id)
        return _to_out(pack, await _mod_names(session, [it.mod_guid for it in pack.items]))

    name = body.name
    if existing is not None:  # on_conflict == "rename"
        name = await _unique_import_name(session, body.name)
    pack = Modpack(name=name, description=body.description)
    pack.items.extend(
        ModpackItem(mod_guid=item.mod_guid, load_order=item.load_order)
        for item in body.items
    )
    session.add(pack)
    try:
        await session.flush()
        pack_id = pack.id
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _integrity_message(exc))
    pack = await _load(session, pack_id)
    return _to_out(pack, await _mod_names(session, [it.mod_guid for it in pack.items]))
