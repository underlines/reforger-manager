"""``mod_sync`` job — scan the local addon cache, then enrich from the Workshop
API. Registered as a job kind in ``main.py``.

Three passes:

1. **Scan + local upsert.** :func:`scanner.scan_all` -> one ``mods`` row per
   directory (guid PK). ``installed_version`` / ``size`` / ``name`` / ``summary``
   / ``tags`` / ``is_unlisted`` from local metadata, ``is_local = True``,
   ``last_checked = NULL`` (the enrich pass stamps it). Each mod's
   ``mod_dependencies`` rows are replaced with ``source = "gproj"`` edges from
   ``addon.gproj``; its ``mod_scenarios`` rows are replaced with the offline
   ``strings``-scan results (name / mode / count still NULL).

2. **Enrich** (rate-limited, best-effort per mod) via :func:`enrich_one`:
   ``get_mod`` + ``get_versions`` + ``get_scenarios`` + ``get_dependencies``.
   Success -> ``api_state = "ok"``, ``latest_*`` filled, API scenarios/deps
   upserted (deps as ``source = "api"``, keeping any gproj-only edges).
   ``ModNotFound`` -> ``api_state = "not_found"`` and the gproj data is left
   untouched. A transient error is logged and skipped.

3. **Prune local flag.** ``mods`` rows whose guid was not in this scan get
   ``is_local = False`` — never deleted (a server definition or a URL-added mod
   may still reference them).
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import SessionLocal
from ..core.jobs import JobContext
from ..models import Mod, ModDependency, ModScenario
from ..models.base import ApiState, _utcnow
from .scanner import ScannedMod, scan_all
from .workshop import ModNotFound, WorkshopClient, WorkshopError, workshop

logger = logging.getLogger("reforger.mods.sync")


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- local upsert
async def _upsert_local(session: AsyncSession, sm: ScannedMod) -> None:
    row = await session.get(Mod, sm.guid)
    if row is None:
        row = Mod(guid=sm.guid)
        session.add(row)

    row.installed_version = sm.version
    row.size = sm.size
    if sm.name:
        row.name = sm.name
    if sm.summary:
        row.summary = sm.summary
    row.tags = sm.tags or row.tags or []
    row.is_unlisted = sm.is_unlisted
    row.is_local = True
    row.last_checked = None
    if sm.has_thumbnail and not row.thumbnail:
        row.thumbnail = "thumbnail.png"  # local sentinel; enrich replaces with a URL

    # Replace this mod's dependency edges with the gproj list.
    await session.execute(
        delete(ModDependency).where(ModDependency.mod_guid == sm.guid)
    )
    for dep_guid in sm.dep_guids:
        session.add(
            ModDependency(
                mod_guid=sm.guid,
                depends_on_guid=dep_guid,
                depends_on_name=None,
                source="gproj",
            )
        )

    # Replace this mod's scenarios with the offline scan.
    await session.execute(
        delete(ModScenario).where(ModScenario.mod_guid == sm.guid)
    )
    for game_id, _path in sm.scenarios:
        session.add(ModScenario(mod_guid=sm.guid, game_id=game_id))


# ------------------------------------------------------------------ enrich
async def enrich_one(
    session: AsyncSession,
    guid: str,
    *,
    client: WorkshopClient | None = None,
) -> Mod:
    """Enrich one ``mods`` row from the Workshop API. Flushes but does not
    commit — the caller owns the transaction. Reused by ``POST /api/mods/add``.

    Raises :class:`WorkshopError` only for *transient* failures; a 404 is
    swallowed and recorded as ``api_state = not_found``.
    """
    client = client or workshop
    guid = guid.upper()
    row = await session.get(Mod, guid)
    if row is None:
        row = Mod(guid=guid, is_local=False)
        session.add(row)

    now = _utcnow()
    try:
        mod = await client.get_mod(guid)
        versions = await client.get_versions(guid)
        api_scenarios = await client.get_scenarios(guid)
        api_deps = await client.get_dependencies(guid)
    except ModNotFound:
        row.api_state = ApiState.not_found
        row.api_checked_at = now
        await session.flush()
        return row

    # ---- success --------------------------------------------------------
    row.api_state = ApiState.ok
    row.api_checked_at = now
    row.last_checked = now

    if versions:
        v0 = versions[0]
        row.latest_version = v0.get("version") or row.latest_version
        row.latest_game_version = v0.get("game_version") or row.latest_game_version

    # Package size in bytes. Only the disk scanner used to set this, so a mod
    # added via POST /api/mods/add but never downloaded kept size = NULL and the
    # free-space guard (S11/S13) then refused every first-time download. Prefer
    # the Workshop mod object's own size, fall back to the newest version's.
    api_size = mod.get("size")
    if api_size is None and versions:
        api_size = versions[0].get("size")
    if api_size is not None:
        try:
            row.size = int(api_size)
        except (TypeError, ValueError):
            pass

    if "unlisted" in mod:
        row.is_unlisted = bool(mod.get("unlisted"))
    if "private" in mod:
        row.is_private = bool(mod.get("private"))
    if "obsolete" in mod:
        row.is_obsolete = bool(mod.get("obsolete"))

    if not row.name:
        row.name = mod.get("name")
    if mod.get("summary"):
        row.summary = mod.get("summary")
    if isinstance(mod.get("tags"), list):
        row.tags = [str(t) for t in mod["tags"]]

    thumb = mod.get("image_url")
    if not thumb:
        previews = mod.get("preview_images")
        if isinstance(previews, list) and previews:
            thumb = previews[0]
    if thumb:
        row.thumbnail = thumb

    await _upsert_api_scenarios(session, guid, api_scenarios)
    await _upsert_api_dependencies(session, guid, api_deps)
    await _ensure_dependency_stub_rows(session, guid)

    await session.flush()
    return row


async def _ensure_dependency_stub_rows(session: AsyncSession, mod_guid: str) -> None:
    """Make sure every dependency of ``mod_guid`` has its own ``mods`` row.

    Without this, a dependency-only addon (RHS content packs, a shared core lib)
    is invisible in the library and the server / modpack mod pickers until a full
    ``mod_sync`` scans its directory. Stub rows carry the name from the API
    dependency record and ``is_local`` from the on-disk addon dir; a later
    enrich / scan fills the rest. Existing rows are only ever upgraded, never
    downgraded.
    """
    from .resolve import ENGINE_BUILTIN_GUIDS
    from .scanner import resolve_addon_dir

    edges = (
        await session.execute(
            select(ModDependency.depends_on_guid, ModDependency.depends_on_name).where(
                ModDependency.mod_guid == mod_guid
            )
        )
    ).all()
    for dep_guid, dep_name in edges:
        dep_guid = dep_guid.upper()
        if dep_guid == mod_guid.upper() or dep_guid in ENGINE_BUILTIN_GUIDS:
            continue
        on_disk = resolve_addon_dir(dep_guid) is not None
        drow = await session.get(Mod, dep_guid)
        if drow is None:
            session.add(Mod(guid=dep_guid, name=dep_name, is_local=on_disk))
        else:
            if dep_name and not drow.name:
                drow.name = dep_name
            if on_disk and not drow.is_local:
                drow.is_local = True


async def _upsert_api_scenarios(
    session: AsyncSession, guid: str, api_scenarios: list[dict]
) -> None:
    existing = {
        s.game_id: s
        for s in (
            await session.execute(
                select(ModScenario).where(ModScenario.mod_guid == guid)
            )
        ).scalars().all()
    }
    api_ids: set[str] = set()
    for s in api_scenarios:
        game_id = s.get("game_id")
        if not game_id:
            continue
        api_ids.add(game_id)
        name = s.get("name")
        game_mode = s.get("game_mode")
        player_count = _to_int(s.get("player_count"))
        hit = existing.get(game_id)
        if hit is not None:
            hit.name = name
            hit.game_mode = game_mode
            hit.player_count = player_count
        else:
            session.add(
                ModScenario(
                    mod_guid=guid,
                    game_id=game_id,
                    name=name,
                    game_mode=game_mode,
                    player_count=player_count,
                )
            )
    # Drop offline-only placeholder rows the API did not confirm, so the
    # scenario picker is not cluttered with unnamed duplicates. Rows carrying a
    # name/mode (i.e. previously enriched) are kept.
    if api_ids:
        for game_id, srow in existing.items():
            if game_id not in api_ids and srow.name is None and srow.game_mode is None:
                await session.delete(srow)


async def _upsert_api_dependencies(
    session: AsyncSession, guid: str, api_deps: list[dict]
) -> None:
    existing = {
        d.depends_on_guid: d
        for d in (
            await session.execute(
                select(ModDependency).where(ModDependency.mod_guid == guid)
            )
        ).scalars().all()
    }
    for d in api_deps:
        dep_guid = d.get("id")
        if not dep_guid:
            continue
        dep_guid = str(dep_guid).upper()
        hit = existing.get(dep_guid)
        if hit is not None:
            # Upgrade the gproj edge in place -> richer "api" row wins.
            hit.source = "api"
            hit.depends_on_name = d.get("name") or hit.depends_on_name
            hit.required_version = d.get("version") or hit.required_version
        else:
            session.add(
                ModDependency(
                    mod_guid=guid,
                    depends_on_guid=dep_guid,
                    depends_on_name=d.get("name"),
                    required_version=d.get("version"),
                    source="api",
                )
            )
    # gproj-only edges (in `existing`, not in the API set) are left as-is.


# --------------------------------------------------------------- job body
async def refresh_local_mods(guids: list[str]) -> list[str]:
    """Re-scan the addon cache and upsert just the rows for ``guids``.

    Used right after a force-download (``mod_download`` job) so the freshly
    downloaded addon's row flips ``is_local = True`` and picks up its on-disk
    size / version / deps / scenarios without waiting for the next full
    ``mod_sync``. No Workshop enrichment, no ``is_local`` pruning. Returns the
    GUIDs that were found on disk and upserted.
    """
    wanted = {g.upper() for g in guids}
    if not wanted:
        return []
    scan = scan_all()
    found: list[str] = []
    async with SessionLocal() as session:
        for sm in scan.mods:
            if sm.guid.upper() in wanted:
                await _upsert_local(session, sm)
                found.append(sm.guid.upper())
        await session.commit()
    return found


async def run_mod_sync(ctx: JobContext) -> dict:
    await ctx.progress(0.0, "scanning addon cache")
    scan = scan_all()
    await ctx.log(
        f"scanned {len(scan.mods)} addon dir(s); {len(scan.problems)} problem(s)"
    )
    for p in scan.problems:
        await ctx.log(f"  problem: {p.get('dir')}: {p.get('error')}")

    scanned_guids: set[str] = set()

    # --- pass 1: local upsert ---
    async with SessionLocal() as session:
        for sm in scan.mods:
            scanned_guids.add(sm.guid)
            await _upsert_local(session, sm)
        await session.commit()
    await ctx.progress(5.0, "enriching from Workshop API")

    # --- pass 2: enrich (best-effort, isolated session per mod) ---
    total = len(scan.mods)
    stats = {"ok": 0, "not_found": 0, "errors": 0}
    for i, sm in enumerate(scan.mods, start=1):
        pct = 5.0 + (90.0 * i / total if total else 90.0)
        await ctx.progress(pct, sm.name or sm.guid)
        try:
            async with SessionLocal() as session:
                row = await enrich_one(session, sm.guid, client=workshop)
                await session.commit()
            state = row.api_state.value if hasattr(row.api_state, "value") else row.api_state
            if state == "ok":
                stats["ok"] += 1
            elif state == "not_found":
                stats["not_found"] += 1
                await ctx.log(f"{sm.name or sm.guid}: not resolvable on the Workshop (404)")
        except WorkshopError as exc:
            stats["errors"] += 1
            await ctx.log(f"{sm.name or sm.guid}: transient API error: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad mod must not abort the job
            stats["errors"] += 1
            logger.exception("enrich failed for %s", sm.guid)
            await ctx.log(f"{sm.name or sm.guid}: enrich error: {type(exc).__name__}: {exc}")

    # --- pass 3: prune the is_local flag ---
    async with SessionLocal() as session:
        stmt = update(Mod).where(Mod.is_local.is_(True)).values(is_local=False)
        if scanned_guids:
            stmt = stmt.where(Mod.guid.notin_(scanned_guids))
        result = await session.execute(stmt)
        await session.commit()
        removed_local = result.rowcount or 0

    summary = {
        "scanned": total,
        "enriched_ok": stats["ok"],
        "not_found": stats["not_found"],
        "errors": stats["errors"],
        "removed_local": removed_local,
        "problems": scan.problems,
    }
    await ctx.progress(100.0, "done")
    await ctx.log(f"mod_sync complete: {summary}")
    return summary
