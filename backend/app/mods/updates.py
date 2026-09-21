"""Mod update application: re-run the engine's headless downloader for every mod
in scope, honouring any explicit pin.

There is no Workshop API call involved: the engine's own downloader is
idempotent, so requesting an addon already at the target version is a
no-op transfer. That single primitive covers "update all" correctly.

The public functions open their own database session so they can be used by a
route or scheduler.  Job factories capture the requested scope because a
``JobContext`` contains only a job id, not persisted job parameters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..core.db import SessionLocal
from ..models import Mod, Server, ServerMod
from .downloader import run_mod_download
from .sync import refresh_local_mods

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..core.jobs import JobContext


MOD_UPDATE_APPLY_JOB_KIND = "mod_update_apply"

UpdateScope = str | int
JobFactory = Callable[["JobContext"], Awaitable[dict[str, Any]]]


class UpdateScopeError(ValueError):
    """The update scope is neither ``all`` nor a positive server id."""


def normalize_scope(scope: UpdateScope) -> str | int:
    """Canonicalize the public ``all``/server-id scope form."""
    if isinstance(scope, bool):
        raise UpdateScopeError("scope must be 'all' or a positive server id")
    if isinstance(scope, int) and scope > 0:
        return scope
    if isinstance(scope, str):
        value = scope.strip()
        if value.lower() == "all":
            return "all"
        if value.isdecimal() and int(value) > 0:
            return int(value)
    raise UpdateScopeError("scope must be 'all' or a positive server id")


async def refresh_mods(scope: UpdateScope, ctx: "JobContext | None" = None) -> dict[str, Any]:
    """Re-download every mod in scope to its latest version (pins honoured).

    No Workshop API calls: the engine's own downloader is idempotent, so an
    addon already at the target version is a no-op.
    """
    normalized = normalize_scope(scope)
    async with SessionLocal() as session:
        targets = await _targets_for_scope(session, normalized)

    guids: list[str] = []
    versions: dict[str, str] = {}
    unavailable: list[str] = []
    for target in targets:
        mod = target["mod"]
        if isinstance(mod, _MissingMod):
            unavailable.append(mod.guid)
            continue
        guid = mod.guid.upper()
        guids.append(guid)
        pin = target["pin"]
        if pin:
            version = pin.get("version")
            if version is not None:
                versions[guid] = version

    if not guids:
        return {
            "scope": normalized,
            "requested": guids,
            "pinned": versions,
            "downloaded": {"guids": [], "progress": 100.0},
            "refreshed": [],
            "unavailable": unavailable,
        }

    downloaded = await run_mod_download(ctx, guids, versions)
    refreshed = await refresh_local_mods(guids)
    return {
        "scope": normalized,
        "requested": guids,
        "pinned": versions,
        "downloaded": downloaded,
        "refreshed": refreshed,
        "unavailable": unavailable,
    }


def make_apply_updates_job(scope: UpdateScope) -> JobFactory:
    """Return a JobManager-compatible closure with a captured validated scope."""
    normalized = normalize_scope(scope)

    async def job(ctx: "JobContext") -> dict[str, Any]:
        return await refresh_mods(normalized, ctx)

    return job


async def _targets_for_scope(session: "AsyncSession", scope: str | int) -> list[dict[str, Any]]:
    if scope == "all":
        mods = (
            await session.execute(select(Mod).where(Mod.is_local.is_(True)).order_by(Mod.guid))
        ).scalars().all()
        server_pins = (
            await session.execute(
                select(ServerMod).where(ServerMod.enabled.is_(True), ServerMod.pinned_version.is_not(None))
            )
        ).scalars().all()
        pins_by_guid: dict[str, list[dict[str, Any]]] = {}
        for server_mod in server_pins:
            pins_by_guid.setdefault(server_mod.mod_guid.upper(), []).append(
                _pin("server", server_mod.pinned_version, server_mod.server_id)
            )
        return [
            {
                "mod": mod,
                # A global cache update must not overwrite a version pinned by
                # any enabled server definition.
                "pin": _pin("library", mod.pinned_version) if mod.pinned_version else (
                    {"source": "server", "pins": pins_by_guid[mod.guid.upper()]}
                    if mod.guid.upper() in pins_by_guid else None
                ),
                "server_id": None,
            }
            for mod in mods
        ]

    server = await session.get(Server, scope)
    if server is None:
        raise UpdateScopeError(f"server {scope} was not found")
    rows = (
        await session.execute(
            select(ServerMod, Mod)
            .outerjoin(Mod, Mod.guid == ServerMod.mod_guid)
            .where(ServerMod.server_id == scope, ServerMod.enabled.is_(True))
            .order_by(ServerMod.load_order, ServerMod.id)
        )
    ).all()
    targets: list[dict[str, Any]] = []
    for server_mod, mod in rows:
        if mod is None:
            # Preserve this configuration error as a result row without making
            # a Workshop request for a mod absent from the library.
            targets.append({"mod": _MissingMod(server_mod.mod_guid, server_mod.mod_name), "pin": None, "server_id": scope})
            continue
        pin = (
            _pin("server", server_mod.pinned_version, scope)
            if server_mod.pinned_version
            else _pin("library", mod.pinned_version)
            if mod.pinned_version
            else None
        )
        targets.append({"mod": mod, "pin": pin, "server_id": scope})
    return targets


class _MissingMod:
    """Small read-only stand-in used to report a dangling server assignment."""

    def __init__(self, guid: str, name: str | None) -> None:
        self.guid = guid
        self.name = name
        self.installed_version = None
        self.latest_version = None


def _pin(source: str, version: str | None, server_id: int | None = None) -> dict[str, Any]:
    pin: dict[str, Any] = {"source": source, "version": version}
    if server_id is not None:
        pin["server_id"] = server_id
    return pin
