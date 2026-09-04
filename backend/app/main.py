"""Reforger Manager API — Phase 2 (backend core).

Lifespan wiring:
  1. schema: Alembic ``upgrade head`` if ``DB_MIGRATE_ON_STARTUP=alembic``,
     otherwise ``Base.metadata.create_all``.
  2. seed the ``engine`` singleton (id=1) from the on-disk appmanifest.
  3. create the bootstrap admin from ADMIN_USERNAME / ADMIN_PASSWORD if the
     users table is empty.
  4. clear stale ``servers.is_running`` flags (a restart orphans child procs).
  5. start the job-queue worker; register job kinds.
  6. fire a background engine-build check (non-blocking).
  7. seed the ``app_settings`` singleton (id=1) from env defaults and start the
     nightly check scheduler in its persisted state.

Auth: every ``/api/*`` route requires a Bearer JWT except ``/api/health`` and
``/api/auth/login`` (enforced via per-router dependencies).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route
from sqlalchemy import func, select

from .api import auth as auth_api
from .api import engine as engine_api
from .api import health as health_api
from .api import jobs as jobs_api
from .api import mcp_tokens as mcp_tokens_api
from .api import mods as mods_api
from .api import modpacks as modpacks_api
from .api import servers as servers_api
from .api import server_files as server_files_api
from .api import settings as settings_api
from .api import storage as storage_api
from .api import scenarios as scenarios_api
from .api import backup as backup_api
from .core.app_settings import get_app_settings
from .core.config import settings
from .core.db import Base, SessionLocal, engine
from .core.jobs import JobContext, job_manager
from .core.security import hash_password
from . import models  # noqa: F401  (imports every mapped class -> registers metadata)
from .models import Job, User
from .mods.downloader import (
    MOD_DOWNLOAD_JOB_KIND,
    expected_closure,
    partition_present,
    run_mod_download,
)
from .mods.post_engine_update import ENGINE_POST_UPDATE_JOB_KIND, on_engine_updated
from .mods.schedule import NightlyCheckScheduler
from .mods.sync import refresh_local_mods, run_mod_sync
from .mods.updates import (
    MOD_UPDATE_APPLY_JOB_KIND,
    MOD_UPDATE_CHECK_JOB_KIND,
    make_apply_updates_job,
    make_check_updates_job,
)
from .mods.verify import VERIFY_REPAIR_JOB_KIND, run_verify_repair
from .mods.workshop import workshop as workshop_client
from .mcp import mcp as mcp_server
from .mcp import mcp_app
from .servers.supervisor import supervisor
from .steam.engine import refresh_engine, seed_engine
from .steam.steamcmd import install_or_update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("reforger.main")


# ------------------------------------------------------- nightly scheduler singleton
# Module-level so PATCH /api/settings can start/stop it at runtime (fact #4).
# ``lifespan`` only sets the initial state from the persisted app_settings row
# and stops it on shutdown; the settings route owns runtime changes.
nightly_scheduler: NightlyCheckScheduler | None = None


async def apply_nightly_settings(enabled: bool, hour: int) -> None:
    """(Re)create the nightly scheduler singleton to match persisted settings."""
    global nightly_scheduler
    if nightly_scheduler is not None:
        await nightly_scheduler.stop()
        nightly_scheduler = None
        logger.info("nightly check scheduler stopped")
    if enabled:
        nightly_scheduler = NightlyCheckScheduler(enabled=True, hour=hour)
        started = await nightly_scheduler.start()
        logger.info(
            "nightly check scheduler started (hour=%s, task_running=%s)", hour, started
        )


# --------------------------------------------------------------------- schema
async def _create_all() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("schema ensured via Base.metadata.create_all")


def _alembic_upgrade_blocking() -> None:
    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


async def _ensure_schema() -> None:
    if settings.db_migrate_on_startup.lower() == "alembic":
        logger.info("running Alembic upgrade head")
        await asyncio.get_running_loop().run_in_executor(None, _alembic_upgrade_blocking)
    else:
        await _create_all()


# ---------------------------------------------------------------- bootstrap
async def _bootstrap_admin() -> None:
    async with SessionLocal() as session:
        count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        if count:
            return
        session.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                is_admin=True,
                is_active=True,
            )
        )
        await session.commit()
        logger.info("created bootstrap admin user %r", settings.admin_username)


async def _seed_engine() -> None:
    async with SessionLocal() as session:
        await seed_engine(session)


async def _engine_check_bg() -> None:
    try:
        async with SessionLocal() as session:
            row = await refresh_engine(session)
        logger.info(
            "engine build check: installed=%s latest=%s update_available=%s",
            row.installed_build,
            row.latest_build,
            row.update_available,
        )
    except Exception:  # pragma: no cover - network/env dependent
        logger.warning("startup engine build check failed", exc_info=True)


# --------------------------------------------------------------- job factories
async def _job_engine_update(ctx: JobContext) -> dict:
    await ctx.progress(0.0, "steamcmd app_update 1874900 validate")
    result = await install_or_update(ctx, validate=True)
    async with SessionLocal() as session:
        result["post_update"] = await on_engine_updated(session)
        await session.commit()
    result["installed_build"] = result["post_update"]["engine"]["installed_build"]
    return result


async def _job_steam_validate(ctx: JobContext) -> dict:
    return await install_or_update(ctx, validate=True)


async def _job_params(ctx: JobContext) -> dict:
    """Load persisted parameters so registered jobs have stable semantics."""
    async with SessionLocal() as session:
        job = await session.get(Job, ctx.job_id)
        params = job.params if job is not None else None
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("job parameters must be an object")
    return params


async def _job_mod_download(ctx: JobContext) -> dict:
    params = await _job_params(ctx)
    guids, versions = params.get("guids", []), params.get("versions")
    if not isinstance(guids, list) or not all(isinstance(guid, str) for guid in guids):
        raise ValueError("mod_download requires a list of GUIDs")
    if versions is not None and not isinstance(versions, dict):
        raise ValueError("mod_download versions must be an object")
    result = await run_mod_download(ctx, guids, versions)

    # Verify the expected dependency closure actually landed on disk: the
    # engine reports success even when it downloaded nothing (e.g. half-written
    # addon dirs from a crashed run are skipped as "already present"). Retry
    # the missing subset once, then fail the job carrying the missing list.
    expected = await expected_closure(ctx, guids)
    present, missing = partition_present(expected)
    retried: set[str] = set()
    if missing:
        retried = set(missing)
        await ctx.log(f"closure incomplete, retrying {len(missing)}: {sorted(missing)}")
        await run_mod_download(
            ctx,
            sorted(missing),
            {g: v for g, v in (versions or {}).items() if str(g).upper() in retried},
        )
        present, missing = partition_present(expected)
    if missing:
        raise RuntimeError(f"download incomplete; missing on disk: {sorted(missing)}")
    result["missing_after_retry"] = sorted(missing)
    # Reflect the freshly downloaded addons in the library immediately: flip
    # is_local, pick up on-disk size/version — without waiting for the next
    # full mod_sync.
    try:
        refreshed = await refresh_local_mods(sorted({str(g).upper() for g in guids} | retried))
        await ctx.log(f"local library refreshed for {len(refreshed)} addon(s): {refreshed}")
        result["refreshed_local"] = refreshed
    except Exception as exc:  # noqa: BLE001 - the download itself already succeeded
        await ctx.log(f"post-download local refresh failed: {type(exc).__name__}: {exc}")
    return result


async def _job_verify_repair(ctx: JobContext) -> dict:
    guids = (await _job_params(ctx)).get("guids")
    if guids is not None and (not isinstance(guids, list) or not all(isinstance(guid, str) for guid in guids)):
        raise ValueError("verify_repair guids must be a list")
    return await run_verify_repair(ctx, guids)


async def _job_update_check(ctx: JobContext) -> dict:
    return await make_check_updates_job((await _job_params(ctx)).get("scope", "all"))(ctx)


async def _job_update_apply(ctx: JobContext) -> dict:
    return await make_apply_updates_job((await _job_params(ctx)).get("scope", "all"))(ctx)


async def _job_engine_post_update(ctx: JobContext) -> dict:
    async with SessionLocal() as session:
        result = await on_engine_updated(session)
        await session.commit()
    return result


# ------------------------------------------------------------------- lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    global nightly_scheduler
    await _ensure_schema()
    await _seed_engine()
    await _bootstrap_admin()
    await supervisor.reconcile_on_startup()

    job_manager.register("engine_update", _job_engine_update)
    job_manager.register("engine_install", _job_engine_update)
    job_manager.register("steam_validate", _job_steam_validate)
    job_manager.register("mod_sync", run_mod_sync)
    job_manager.register(MOD_DOWNLOAD_JOB_KIND, _job_mod_download)
    job_manager.register(VERIFY_REPAIR_JOB_KIND, _job_verify_repair)
    job_manager.register(MOD_UPDATE_CHECK_JOB_KIND, _job_update_check)
    job_manager.register(MOD_UPDATE_APPLY_JOB_KIND, _job_update_apply)
    job_manager.register(ENGINE_POST_UPDATE_JOB_KIND, _job_engine_post_update)
    await job_manager.start()

    # Initial nightly-check state: the persisted app_settings singleton (lazily
    # seeded from env defaults on first boot).
    async with SessionLocal() as session:
        persisted_settings = await get_app_settings(session)
        await session.commit()
    await apply_nightly_settings(
        persisted_settings.nightly_check_enabled, persisted_settings.nightly_check_hour
    )

    if settings.engine_check_on_startup:
        asyncio.create_task(_engine_check_bg())

    logger.info("Reforger Manager backend ready")
    try:
        # The MCP session manager's task group must be entered exactly once per
        # process; Starlette does not propagate lifespan events to mounts, so
        # the /mcp mount cannot start it itself.
        async with mcp_server.session_manager.run():
            yield
    finally:
        if nightly_scheduler is not None:
            await nightly_scheduler.stop()
            nightly_scheduler = None
        await job_manager.stop()
        await workshop_client.aclose()
        if supervisor.is_running():
            logger.warning("backend shutting down while server %s runs", supervisor.active_server_id)


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_API = "/api"
app.include_router(health_api.router, prefix=_API)
app.include_router(auth_api.router, prefix=_API)
app.include_router(engine_api.router, prefix=_API)
app.include_router(jobs_api.router, prefix=_API)
app.include_router(mcp_tokens_api.router, prefix=_API)
app.include_router(mods_api.router, prefix=_API)
app.include_router(modpacks_api.router, prefix=_API)
app.include_router(servers_api.router, prefix=_API)
app.include_router(server_files_api.router, prefix=_API)
app.include_router(settings_api.router, prefix=_API)
app.include_router(storage_api.router, prefix=_API)
app.include_router(scenarios_api.router, prefix=_API)
app.include_router(backup_api.router, prefix=_API)

# MCP server (sprint 4): streamable-HTTP transport at <host>/mcp, bearer-token
# gated. A plain Route (not a Mount): Starlette Mount paths compile to
# "<path>/{path}", so Mount("/mcp") matches "/mcp/..." but never the exact
# "/mcp" URL MCP clients use. Registered with the routers, ahead of the SPA
# catch-all (fact #1).
app.router.routes.append(Route("/mcp", mcp_app(), include_in_schema=False))


# Phase 4 mounts the built SPA here. Guarded so it is a no-op until frontend/dist
# exists in the image. The client uses history-mode routing (BrowserRouter), so
# every non-API path that is not a real static file must fall back to index.html
# — otherwise a page refresh or deep link 404s.
_SPA_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _SPA_DIR.is_dir():
    _SPA_INDEX = _SPA_DIR / "index.html"
    if (_SPA_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_SPA_DIR / "assets")), name="spa-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str) -> FileResponse:
        if full_path.startswith(("api", "docs", "openapi", "mcp")):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (_SPA_DIR / full_path).resolve()
        if full_path and candidate.is_file() and _SPA_DIR.resolve() in candidate.parents:
            return FileResponse(str(candidate))
        return FileResponse(str(_SPA_INDEX))
else:  # pragma: no cover

    @app.get("/")
    async def _root() -> dict:
        return {"app": settings.app_name, "version": settings.app_version, "docs": "/docs"}
