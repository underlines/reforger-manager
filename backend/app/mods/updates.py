"""API-only mod update discovery and guarded update application.

The public functions open their own database session so they can be used by a
route or scheduler.  Job factories capture the requested scope because a
``JobContext`` contains only a job id, not persisted job parameters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..core.db import SessionLocal
from ..models import ApiState, Mod, Server, ServerMod
from .downloader import run_mod_download
from .workshop import ModNotFound, WorkshopError, workshop

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..core.jobs import JobContext


MOD_UPDATE_CHECK_JOB_KIND = "mod_update_check"
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


async def check_updates(scope: UpdateScope) -> dict[str, Any]:
    """Check installed target mods against Workshop metadata without downloading."""
    normalized = normalize_scope(scope)
    async with SessionLocal() as session:
        result = await _check_updates(session, normalized)
        await session.commit()
        return result


async def apply_updates(scope: UpdateScope) -> dict[str, Any]:
    """Download every currently available, unpinned update in ``scope``.

    This direct helper intentionally has no progress context.  Routes should
    enqueue :func:`make_apply_updates_job` instead so the downloader can report
    its progress through its real ``JobContext``.
    """
    normalized = normalize_scope(scope)
    async with SessionLocal() as session:
        result = await _check_updates(session, normalized)
        await session.commit()
    return await _apply_result(result, None)


def make_check_updates_job(scope: UpdateScope) -> JobFactory:
    """Return a JobManager-compatible closure with a captured validated scope."""
    normalized = normalize_scope(scope)

    async def job(ctx: "JobContext") -> dict[str, Any]:
        async with SessionLocal() as session:
            result = await _check_updates(session, normalized, ctx)
            await session.commit()
            return result

    return job


def make_apply_updates_job(scope: UpdateScope) -> JobFactory:
    """Return a job closure which checks first, then downloads allowed updates."""
    normalized = normalize_scope(scope)

    async def job(ctx: "JobContext") -> dict[str, Any]:
        async with SessionLocal() as session:
            result = await _check_updates(session, normalized, ctx)
            await session.commit()
        return await _apply_result(result, ctx)

    return job


async def _check_updates(
    session: "AsyncSession", scope: str | int, ctx: "JobContext | None" = None
) -> dict[str, Any]:
    targets = await _targets_for_scope(session, scope)
    result: dict[str, Any] = {
        "scope": scope,
        "available_updates": [],
        "skipped_pins": [],
        "unavailable": [],
        "errors": [],
        "checked": len(targets),
    }
    if ctx:
        await ctx.progress(0.0, f"checking {len(targets)} mod(s)")

    for index, target in enumerate(targets, start=1):
        mod = target["mod"]
        item = _item_base(mod, target)
        if isinstance(mod, _MissingMod):
            result["unavailable"].append({**item, "reason": "not_in_library"})
        else:
            try:
                remote = await workshop.get_mod(mod.guid)
            except ModNotFound:
                mod.api_state = ApiState.not_found
                mod.api_checked_at = datetime.now(UTC)
                result["unavailable"].append({**item, "reason": "not_resolvable"})
            except WorkshopError as exc:
                result["errors"].append({**item, "reason": "workshop_error", "detail": str(exc)})
            else:
                _record_workshop_state(mod, remote)
                latest = mod.latest_version
                if not mod.installed_version:
                    result["unavailable"].append({**item, "reason": "not_installed", "latest_version": latest})
                elif target["pin"]:
                    result["skipped_pins"].append(
                        {**item, "latest_version": latest, "pin": target["pin"]}
                    )
                elif latest and latest != mod.installed_version:
                    result["available_updates"].append(
                        {**item, "latest_version": latest, "target_version": latest}
                    )
        if ctx:
            await ctx.progress(index * 100.0 / len(targets), f"checked {mod.name or mod.guid}")
    return result


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


def _item_base(mod: Mod | _MissingMod, target: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "guid": mod.guid,
        "name": mod.name,
        "installed_version": mod.installed_version,
    }
    if target["server_id"] is not None:
        item["server_id"] = target["server_id"]
    return item


def _record_workshop_state(mod: Mod, remote: dict[str, Any]) -> None:
    mod.latest_version = remote.get("version") or mod.latest_version
    mod.latest_game_version = remote.get("game_version") or mod.latest_game_version
    mod.name = remote.get("name") or mod.name
    mod.is_unlisted = bool(remote.get("unlisted", False))
    mod.is_private = bool(remote.get("private", False))
    mod.is_obsolete = bool(remote.get("obsolete", False))
    mod.api_state = ApiState.ok
    checked_at = datetime.now(UTC)
    mod.api_checked_at = checked_at
    mod.last_checked = checked_at

    # Self-heal: a never-downloaded mod keeps size = NULL, which the free-space
    # guard then refuses. Backfill from the payload we already hold (mirroring
    # the enrich_one chain) ONLY when it is NULL — never clobber a known size.
    if mod.size is None:
        api_size = remote.get("size")
        if api_size is None:
            versions = remote.get("versions")
            if isinstance(versions, list) and versions:
                api_size = versions[0].get("size")
        if api_size is not None:
            try:
                mod.size = int(api_size)
            except (TypeError, ValueError):
                pass


async def _apply_result(result: dict[str, Any], ctx: "JobContext | None") -> dict[str, Any]:
    updates = result["available_updates"]
    versions = {row["guid"].upper(): row["target_version"] for row in updates}
    if not versions:
        result["applied"] = {"guids": [], "versions": {}, "progress": 100.0, "downloaded": {}}
        return result
    result["applied"] = await run_mod_download(ctx, list(versions), versions)
    return result
