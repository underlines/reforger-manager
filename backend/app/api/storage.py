# register in main.py: app.include_router(storage_api.router, prefix=_API)
"""Storage view: on-disk mod sizes, free space, and orphan management (S12).

GET /api/storage -> {mods_path, free_bytes, total_bytes, per_mod, orphans,
                      kept_as_dependency, unreferenced_entries}

``unreferenced_entries`` are library rows with no on-disk files and no
references at all (not in ``server_mods`` / ``modpack_items`` / any dependency
closure). They are invisible to the disk-oriented ``orphans`` list yet cannot be
re-downloaded away, so they are surfaced here for deletion via
``DELETE /api/mods/{guid}``.

An orphan is ``is_local`` AND absent from ``server_mods`` AND absent from
``modpack_items`` AND absent from the resolved dependency closure of (all
assigned guids UNION all packed guids). The closure term is the whole story:
a dependency-only mod (RHS base, CBA-like) appears in no ``server_mods`` row,
so the naive definition would offer to delete files a working definition needs.
Such mods are reported separately as ``kept_as_dependency`` with the parents
that hold them (S12 watch-out). The closure is resolved offline (DB-only) so
this route never fans out to the Workshop API.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_session
from ..core.security import get_current_user
from ..models import Mod, ModpackItem, ServerMod
from ..mods.resolve import resolve_dependencies
from ..mods.scanner import addons_root, resolve_addon_dir
from ..schemas.storage import (
    StorageKeptDependency,
    StorageOrphan,
    StorageOut,
    StoragePerMod,
    StorageUnreferencedEntry,
)

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(get_current_user)])


async def orphan_reference_sets(
    session: AsyncSession,
) -> tuple[set[str], dict[str, set[str]]]:
    """Directly referenced guids plus per-guid dependency-closure owners.

    Returns ``(directly_referenced, closure_owners)`` where ``closure_owners[g]``
    is the set of direct-reference guids whose resolved dependency closure
    contains ``g``. Every root is a node in its own closure, so the closure
    always covers ``directly_referenced``.
    """
    assigned = set(
        (await session.execute(select(ServerMod.mod_guid))).scalars().all()
    )
    packed = set(
        (await session.execute(select(ModpackItem.mod_guid))).scalars().all()
    )
    roots = {str(g).upper() for g in assigned | packed}
    closure_owners: dict[str, set[str]] = {}
    for root in roots:
        tree = await resolve_dependencies(session, [root], use_api=False)
        for node in tree.nodes:
            closure_owners.setdefault(node.guid, set()).add(root)
    return roots, closure_owners


def _dir_bytes(path: Path) -> int:
    """Total size in bytes of everything under ``path`` (0 when absent)."""
    if not path.is_dir():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += (Path(dirpath) / filename).stat().st_size
            except OSError:
                continue
    return total


def _bytes_for(guid: str) -> int:
    addon_dir = resolve_addon_dir(guid)
    return _dir_bytes(addon_dir) if addon_dir is not None else 0


@router.get("", response_model=StorageOut)
async def get_storage(session: AsyncSession = Depends(get_session)) -> StorageOut:
    usage = shutil.disk_usage(settings.mods_dir)
    directly_referenced, closure_owners = await orphan_reference_sets(session)
    closure = set(closure_owners)

    all_mods = list((await session.execute(select(Mod))).scalars().all())
    local_mods = [mod for mod in all_mods if mod.is_local]
    by_guid = {mod.guid: mod for mod in local_mods}

    per_mod = [
        StoragePerMod(guid=mod.guid, name=mod.name, bytes=_bytes_for(mod.guid))
        for mod in local_mods
    ]
    per_mod.sort(key=lambda item: item.bytes, reverse=True)

    orphans: list[StorageOrphan] = []
    kept: list[StorageKeptDependency] = []
    for guid in sorted(by_guid):
        mod = by_guid[guid]
        if guid in directly_referenced or guid in closure:
            continue
        orphans.append(StorageOrphan(guid=guid, name=mod.name, bytes=_bytes_for(guid)))
    for guid in sorted(closure_owners):
        mod = by_guid.get(guid)
        if mod is None or guid in directly_referenced:
            continue
        kept.append(
            StorageKeptDependency(
                guid=guid,
                name=mod.name,
                bytes=_bytes_for(guid),
                required_by=sorted(closure_owners[guid]),
            )
        )
    kept.sort(key=lambda item: len(item.required_by), reverse=True)

    unreferenced_entries = [
        StorageUnreferencedEntry(guid=mod.guid, name=mod.name)
        for mod in sorted(all_mods, key=lambda m: m.guid)
        if not mod.is_local
        and mod.guid not in directly_referenced
        and mod.guid not in closure
    ]

    return StorageOut(
        mods_path=str(settings.mods_dir),
        free_bytes=usage.free,
        total_bytes=usage.total,
        per_mod=per_mod,
        orphans=orphans,
        kept_as_dependency=kept,
        unreferenced_entries=unreferenced_entries,
    )