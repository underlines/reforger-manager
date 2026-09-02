# register in main.py: app.include_router(scenarios_api.router, prefix=_API)
"""Scenario picker — resolve a set of mod GUIDs into their scenarios (S6, §A4).

POST /api/scenarios/resolve  {guids: [...]}
  -> {scenarios: [{game_id, name, game_mode, player_count, mod_guid, mod_name,
                   source: "db"|"api"}], failed: [{guid, reason}]}

DB-first by design (fact #7): the Workshop client is rate-limited at 60/min
with burst 20, so a 20-mod definition must not become 20 Workshop calls on a
form render. ``mod_scenarios`` rows already cached for a GUID are served as
``source: "db"`` and the API is never touched for that GUID. Only GUIDs with
zero cached rows fan out to ``workshop.get_scenarios``; whatever comes back is
persisted (respecting ``uq_mod_scenario``) and served as ``source: "api"``, so
the next identical request is DB-only. A GUID whose API call fails (404 /
timeout / WorkshopError) is reported in ``failed[]`` and does not fail the
request for the other GUIDs.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.security import get_current_user
from ..models import Mod, ModScenario
from ..mods.workshop import (
    ModNotFound,
    RateLimitExceeded,
    WorkshopError,
    get_scenarios,
)

router = APIRouter(
    prefix="/scenarios", tags=["scenarios"], dependencies=[Depends(get_current_user)]
)


# ------------------------------------------------------------------ schemas
class ScenarioResolveIn(BaseModel):
    guids: list[str] = []


class ScenarioOut(BaseModel):
    game_id: str
    name: str | None = None
    game_mode: str | None = None
    player_count: int | None = None
    mod_guid: str
    mod_name: str | None = None
    source: Literal["db", "api"]


class ResolveFailedOut(BaseModel):
    guid: str
    reason: str


class ScenarioResolveOut(BaseModel):
    scenarios: list[ScenarioOut]
    failed: list[ResolveFailedOut]


# ------------------------------------------------------------------ helpers
def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _short_reason(exc: Exception) -> str:
    if isinstance(exc, ModNotFound):
        return "not found on the Workshop"
    if isinstance(exc, RateLimitExceeded):
        return "Workshop rate limit"
    message = str(exc).strip()
    return message[:120] or "Workshop API error"


async def _mod_names(session: AsyncSession, guids: list[str]) -> dict[str, str | None]:
    if not guids:
        return {}
    rows = (
        await session.execute(select(Mod.guid, Mod.name).where(Mod.guid.in_(guids)))
    ).all()
    return {guid: name for guid, name in rows}


async def _load_cached(
    session: AsyncSession, guids: list[str]
) -> dict[str, list[ModScenario]]:
    """Cached ``mod_scenarios`` rows per GUID (empty list when none cached)."""
    if not guids:
        return {}
    rows = (
        await session.execute(select(ModScenario).where(ModScenario.mod_guid.in_(guids)))
    ).scalars().all()
    cached: dict[str, list[ModScenario]] = {guid: [] for guid in guids}
    for row in rows:
        cached.setdefault(row.mod_guid, []).append(row)
    return cached


async def _cache_api_scenarios(
    session: AsyncSession, guid: str, rows: list[dict]
) -> None:
    """Persist API scenario dicts into ``mod_scenarios`` (the DB-first cache).

    ``mod_scenarios.mod_guid`` is an FK to ``mods.guid``, so a GUID without a
    library row gets a bare ``Mod`` row first (same convention as
    ``mods/sync.enrich_one``). The upsert ignores ``(mod_guid, game_id)`` rows
    another concurrent request may already have inserted — no IntegrityError.
    """
    values = [
        {
            "mod_guid": guid,
            "game_id": row["game_id"],
            "name": row.get("name"),
            "game_mode": row.get("game_mode"),
            "player_count": _to_int(row.get("player_count")),
        }
        for row in rows
        if row.get("game_id")
    ]
    if not values:
        return
    if await session.get(Mod, guid) is None:
        session.add(Mod(guid=guid))
        await session.flush()
    await session.execute(
        _mod_scenario_insert(session.get_bind())
        .values(values)
        .on_conflict_do_nothing(index_elements=["mod_guid", "game_id"])
    )


def _mod_scenario_insert(bind):
    """Dialect-specific ``INSERT`` so ``ON CONFLICT ... DO NOTHING`` works on
    both Postgres (prod) and SQLite (tests) — the generic ``sqlalchemy.insert``
    has no ``on_conflict_do_nothing``.
    """
    if bind.dialect.name == "postgresql":
        return postgresql.insert(ModScenario)
    return sqlite_dialect.insert(ModScenario)


def _sort_key(row: dict) -> str:
    return (row.get("name") or row.get("game_id") or "").lower()


# ------------------------------------------------------------------ resolve
@router.post("/resolve", response_model=ScenarioResolveOut)
async def resolve_scenarios(
    body: ScenarioResolveIn,
    session: AsyncSession = Depends(get_session),
) -> ScenarioResolveOut:
    guids = list(
        dict.fromkeys(g.strip().upper() for g in body.guids if g and g.strip())
    )
    if not guids:
        return ScenarioResolveOut(scenarios=[], failed=[])

    names = await _mod_names(session, guids)
    cached = await _load_cached(session, guids)
    missing = [guid for guid in guids if not cached[guid]]

    scenarios: list[ScenarioOut] = []
    for guid in guids:
        for row in cached[guid]:
            scenarios.append(
                ScenarioOut(
                    game_id=row.game_id,
                    name=row.name,
                    game_mode=row.game_mode,
                    player_count=row.player_count,
                    mod_guid=guid,
                    mod_name=names.get(guid),
                    source="db",
                )
            )

    failed: list[ResolveFailedOut] = []
    for guid in missing:
        try:
            api_rows = await get_scenarios(guid)
        except WorkshopError as exc:
            failed.append(ResolveFailedOut(guid=guid, reason=_short_reason(exc)))
            continue
        await _cache_api_scenarios(session, guid, api_rows)
        for row in sorted(api_rows, key=_sort_key):
            if not row.get("game_id"):
                continue
            scenarios.append(
                ScenarioOut(
                    game_id=row["game_id"],
                    name=row.get("name"),
                    game_mode=row.get("game_mode"),
                    player_count=_to_int(row.get("player_count")),
                    mod_guid=guid,
                    mod_name=names.get(guid),
                    source="api",
                )
            )

    await session.commit()
    return ScenarioResolveOut(scenarios=scenarios, failed=failed)