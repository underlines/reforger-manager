"""Post-update engine compatibility and pin-staleness report.

This module intentionally performs no job scheduling.  The engine-update job
invokes it after Steam has finished updating the server installation.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Mod, ServerMod
from ..servers.preflight import preflight_all
from ..steam.engine import mark_engine_updated
from .pinning import is_stale


ENGINE_POST_UPDATE_JOB_KIND = "engine_post_update"


async def on_engine_updated(session: AsyncSession) -> dict:
    """Persist the new engine build and return its compatibility impact.

    The report is deliberately JSON-native so an engine-update job can save it
    directly as its result without additional serialization.
    """
    engine = await mark_engine_updated(session)
    reports = await preflight_all(session)

    library_pins = (
        await session.execute(
            select(Mod).where(Mod.pinned_version.is_not(None)).order_by(Mod.guid)
        )
    ).scalars().all()
    server_pins = (
        await session.execute(
            select(ServerMod).where(ServerMod.pinned_version.is_not(None)).order_by(
                ServerMod.server_id, ServerMod.mod_guid
            )
        )
    ).scalars().all()

    stale_pins = [
        _pin_report("library", pin.guid, pin, engine)
        for pin in library_pins
        if is_stale(pin, engine)
    ]
    stale_pins.extend(
        _pin_report("server", pin.mod_guid, pin, engine, server_id=pin.server_id)
        for pin in server_pins
        if is_stale(pin, engine)
    )

    serialized_reports = {
        str(server_id): report.as_dict() for server_id, report in reports.items()
    }
    verdict_counts = {"green": 0, "warn": 0, "blocked": 0}
    for report in reports.values():
        verdict_counts[report.verdict] = verdict_counts.get(report.verdict, 0) + 1

    return {
        "engine": {
            "installed_build": engine.installed_build,
            "installed_version": engine.installed_version,
        },
        "server_preflight": serialized_reports,
        "stale_pins": stale_pins,
        "summary": {
            "servers_checked": len(reports),
            "green_servers": verdict_counts["green"],
            "warning_servers": verdict_counts["warn"],
            "blocked_servers": verdict_counts["blocked"],
            "library_pins_checked": len(library_pins),
            "server_pins_checked": len(server_pins),
            "stale_pins": len(stale_pins),
        },
    }


def _pin_report(
    scope: str, guid: str, pin: Mod | ServerMod, engine, *, server_id: int | None = None
) -> dict:
    report = {
        "scope": scope,
        "guid": guid,
        "pinned_version": pin.pinned_version,
        "pinned_at_build": pin.pinned_at_build,
        "installed_build": engine.installed_build,
        "guidance": "Unpin and take latest.",
    }
    if server_id is not None:
        report["server_id"] = server_id
    return report
