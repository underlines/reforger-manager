"""Server definitions CRUD + lifecycle.

GET    /api/servers                 list
POST   /api/servers                 create
GET    /api/servers/{id}            read
PATCH  /api/servers/{id}            update (PATCH semantics; `mods` replaces the set)
DELETE /api/servers/{id}            delete (refused while running)
POST   /api/servers/{id}/start      start (single-server rule -> 409)
POST   /api/servers/{id}/stop       stop (SIGTERM -> SIGKILL)
GET    /api/servers/{id}/config     generated config.json (regenerated on the fly)
POST   /api/servers/{id}/config/preview  generated config.json for an unsaved draft
POST   /api/servers/{id}/clone      deep-copy a definition + its mod set (incl. pins)
POST   /api/servers/{id}/schedule-restart   arm a warned restart (in-memory, single schedule)
GET    /api/servers/{id}/schedule-restart   read it (armed:false when stale/gone)
DELETE /api/servers/{id}/schedule-restart   cancel it
WS     /api/servers/{id}/console    live log tail
"""

from __future__ import annotations

import asyncio
import copy
from collections import defaultdict, deque
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.websockets import WebSocketDisconnect

from ..core.db import SessionLocal, get_session
from ..a2s.client import A2SConnectionError, A2SError, A2STimeoutError, query as query_a2s
from ..core.events import broadcaster, server_console_channel
from ..core.jobs import job_manager
from ..core.security import get_current_user
from ..models import Server, ServerMod
from ..mods.logview import (
    current_log,
    is_spam_line,
    iter_log_lines,
    line_severity,
    normalise_severity,
    read_log_download,
    search_log,
)
from ..mods.freespace import guard_update_scope
from ..mods.pinning import CurrentEngineBuildMissing, PinRecordNotFound, pin_server_mod, unpin_server_mod
from ..mods.updates import MOD_UPDATE_APPLY_JOB_KIND, MOD_UPDATE_CHECK_JOB_KIND
from ..rcon.client import RconClient, RconError, RconTimeoutError
from ..schemas.server import (
    ServerCloneIn,
    ServerConfigOut,
    ServerCreate,
    RconCommandIn,
    ScheduleRestartIn,
    ServerModPinIn,
    ServerOut,
    ServerUpdate,
)
from ..schemas.job import JobEnqueuedOut
from ..servers.config_gen import build_config, resolved_mod_entries, write_config
from ..servers.preflight import preflight
from ..servers.supervisor import (
    SingleServerError,
    SupervisorError,
    supervisor,
    _loopback,
)
from .ws import ws_authenticate

router = APIRouter(prefix="/servers", tags=["servers"])
authed = [Depends(get_current_user)]
_STATS_HISTORY: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=60))
_STATS_INTERVAL_SECONDS = 5.0


async def _load(session: AsyncSession, server_id: int) -> Server:
    server = (
        await session.execute(
            select(Server).where(Server.id == server_id).options(selectinload(Server.mods))
        )
    ).scalar_one_or_none()
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "server not found")
    return server


def _apply_mods(server: Server, mods) -> None:
    """Replace ``server.mods`` from a payload, preserving per-server pins.

    The transport is a destructive replace (the whole set is rebuilt), so a
    caller that merely reorders or toggles mods without echoing the pin fields
    would otherwise silently drop every ``pinned_*`` column (fact #1). Index the
    outgoing rows by ``mod_guid`` and, for any GUID that survives the replace,
    carry ``pinned_version`` / ``pinned_at`` / ``pinned_at_build`` /
    ``pinned_reason`` forward -- unless the incoming item explicitly sets
    ``pinned_version`` (a non-None value means the caller is deliberately
    (re)pinning, so the pin fields come from the payload instead). A GUID that is
    absent from the new payload loses its pin along with its row, as intended.
    ``pinned_at`` is never taken from the payload (``ServerModIn`` has no such
    field); it is only ever carried forward from the prior row.
    """
    prior = {sm.mod_guid: sm for sm in server.mods}
    incoming = {m.mod_guid for m in mods}
    # A GUID that left the set loses its row (and its pin with it). Reconcile in
    # place rather than clear-and-rebuild: a surviving GUID keeps the same row,
    # so its pin columns carry forward for free and the unique
    # ``(server_id, mod_guid)`` constraint is never transiently violated.
    for sm in list(server.mods):
        if sm.mod_guid not in incoming:
            server.mods.remove(sm)
    for idx, m in enumerate(mods):
        row = prior.get(m.mod_guid)
        if row is None:
            row = ServerMod(mod_guid=m.mod_guid)
            server.mods.append(row)
        row.mod_name = m.mod_name
        row.load_order = m.load_order if m.load_order is not None else idx
        row.enabled = m.enabled
        if m.pinned_version is not None:
            # Deliberate (re)pin -- take the pin from the payload. ``pinned_at``
            # has no payload field, so it is cleared here rather than kept stale.
            row.pinned_version = m.pinned_version
            row.pinned_at_build = m.pinned_at_build
            row.pinned_reason = m.pinned_reason
            row.pinned_at = None
        elif m.mod_guid not in prior:
            row.pinned_version = None
            row.pinned_at_build = None
            row.pinned_reason = None
            row.pinned_at = None
        # else: a surviving row with no explicit pin keeps its existing columns.


async def _stats(server: Server) -> dict:
    try:
        info = await query_a2s(_loopback(server.a2s_address), server.a2s_port)
    except A2STimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc))
    except (A2SConnectionError, A2SError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    sample = asdict(info)
    _STATS_HISTORY[server.id].append(sample)
    return {"current": sample, "history": list(_STATS_HISTORY[server.id])}


async def _rcon(server: Server, command: str | None = None) -> str | list[dict]:
    if supervisor.active_server_id != server.id or not supervisor.is_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "that server is not the one running")
    if not server.rcon_enabled or not server.rcon_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "RCON is not configured for this server")
    try:
        async with RconClient() as client:
            await client.connect(_loopback(server.rcon_address), server.rcon_port, server.rcon_password)
            return await client.command(command) if command is not None else await client.players()
    except RconTimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except RconError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


@router.get("", response_model=list[ServerOut], dependencies=authed)
async def list_servers(session: AsyncSession = Depends(get_session)) -> list[Server]:
    rows = (
        await session.execute(
            select(Server).options(selectinload(Server.mods)).order_by(Server.id)
        )
    ).scalars().all()
    return list(rows)


@router.post("", response_model=ServerOut, status_code=status.HTTP_201_CREATED, dependencies=authed)
async def create_server(
    body: ServerCreate, session: AsyncSession = Depends(get_session)
) -> Server:
    data = body.model_dump(exclude={"mods"})
    server = Server(**data)
    _apply_mods(server, body.mods)
    session.add(server)
    await session.commit()
    return await _load(session, server.id)


@router.get("/{server_id}", response_model=ServerOut, dependencies=authed)
async def get_server(server_id: int, session: AsyncSession = Depends(get_session)) -> Server:
    return await _load(session, server_id)


@router.patch("/{server_id}", response_model=ServerOut, dependencies=authed)
async def update_server(
    server_id: int, body: ServerUpdate, session: AsyncSession = Depends(get_session)
) -> Server:
    server = await _load(session, server_id)
    patch = body.model_dump(exclude_unset=True)
    mods = patch.pop("mods", None)
    for key, value in patch.items():
        setattr(server, key, value)
    if mods is not None:
        _apply_mods(server, body.mods)
    await session.commit()
    return await _load(session, server_id)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=authed)
async def delete_server(server_id: int, session: AsyncSession = Depends(get_session)) -> None:
    server = await _load(session, server_id)
    if server.is_running or supervisor.active_server_id == server_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "stop the server before deleting it")
    await session.delete(server)
    await session.commit()


# Clone (S7 -- the experiment workflow that replaced config-revision rollback):
# every column NOT in this set is copied verbatim from the source row. Excluded
# are the identity columns the clone must get its own of (id, name, timestamps)
# and everything that would make "edit the copy, delete if bad" unsafe or wrong:
# the config snapshot + revision counter (rollback was cut) and the source's
# runtime lifecycle state.
_CLONE_EXCLUDED_COLUMNS = frozenset(
    {
        "id",  # the clone gets its own PK
        "name",  # comes from the request body
        "created_at",  # fresh timestamps
        "updated_at",
        "config",  # no config.json snapshot
        "config_revision",  # no revision counter
        "is_running",  # runtime state of the source, never the clone
        "pid",
        "last_state",
        "last_exit_code",
        "last_diagnosis",
        "last_started_at",
        "last_stopped_at",
    }
)


@router.post(
    "/{server_id}/clone",
    response_model=ServerOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=authed,
)
async def clone_server(
    server_id: int, body: ServerCloneIn, session: AsyncSession = Depends(get_session)
) -> Server:
    """Deep-copy a definition and its mod set.

    Copies every config/network/RCON/game column (including ``is_favourite``,
    ``game_properties`` and ``extra_config``) and re-creates every ``ServerMod``
    row -- never shared ORM objects -- with all four ``pinned_*`` columns and
    the ``load_order`` preserved. The clone is a fully independent row: editing
    it never touches the source. Ports are copied verbatim, so the clone
    collides with its source on bind/a2s/rcon ports; harmless under the
    single-server rule, and flagged by the config form.
    """
    source = await _load(session, server_id)
    clone = Server(name=body.name)
    for column in Server.__table__.columns:
        if column.name in _CLONE_EXCLUDED_COLUMNS:
            continue
        setattr(clone, column.name, copy.deepcopy(getattr(source, column.name)))
    clone.mods = [
        ServerMod(
            mod_guid=mod.mod_guid,
            mod_name=mod.mod_name,
            load_order=mod.load_order,
            enabled=mod.enabled,
            pinned_version=mod.pinned_version,
            pinned_at=mod.pinned_at,
            pinned_at_build=mod.pinned_at_build,
            pinned_reason=mod.pinned_reason,
        )
        for mod in source.mods
    ]
    session.add(clone)
    await session.commit()
    return await _load(session, clone.id)


@router.get("/{server_id}/config", response_model=ServerConfigOut, dependencies=authed)
async def get_generated_config(
    server_id: int, write: bool = Query(default=False), session: AsyncSession = Depends(get_session)
) -> ServerConfigOut:
    server = await _load(session, server_id)
    config = build_config(server, await resolved_mod_entries(session, server))
    path = write_config(server_id, config) if write else None
    from ..core.config import settings

    return ServerConfigOut(
        server_id=server_id,
        config=config,
        path=str(path or settings.config_path(server_id)),
    )


@router.post("/{server_id}/config/preview", response_model=ServerConfigOut, dependencies=authed)
async def preview_generated_config(
    server_id: int, body: ServerUpdate, session: AsyncSession = Depends(get_session)
) -> ServerConfigOut:
    """Generate the config.json for an *unsaved* draft.

    Applies a ``ServerUpdate`` patch (including a working-copy ``mods`` set) to a
    detached copy of the row and returns the same ``ServerConfigOut`` shape as
    ``GET /{server_id}/config`` -- without persisting anything and without
    writing the config file. The row is ``expunge``-d before it is mutated so a
    later flush in this request cannot write the draft (fact #2).
    """
    server = await _load(session, server_id)
    session.expunge(server)  # detach: mutations below must never reach the DB
    patch = body.model_dump(exclude_unset=True)
    mods = patch.pop("mods", None)
    for key, value in patch.items():
        setattr(server, key, value)
    if mods is not None:
        _apply_mods(server, body.mods)
    from ..core.config import settings

    return ServerConfigOut(
        server_id=server_id,
        config=build_config(server, await resolved_mod_entries(session, server)),
        path=str(settings.config_path(server_id)),
    )


@router.get("/{server_id}/preflight", dependencies=authed)
async def get_preflight(server_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    await _load(session, server_id)
    return (await preflight(session, server_id)).as_dict()


@router.post("/{server_id}/mods/update/check", response_model=JobEnqueuedOut, status_code=status.HTTP_202_ACCEPTED, dependencies=authed)
async def check_server_updates(server_id: int, session: AsyncSession = Depends(get_session)) -> JobEnqueuedOut:
    await _load(session, server_id)
    job_id = await job_manager.enqueue(MOD_UPDATE_CHECK_JOB_KIND, params={"scope": server_id})
    return JobEnqueuedOut(job_id=job_id, kind=MOD_UPDATE_CHECK_JOB_KIND)


@router.post("/{server_id}/mods/update/apply", response_model=JobEnqueuedOut, status_code=status.HTTP_202_ACCEPTED, dependencies=authed)
async def apply_server_updates(server_id: int, session: AsyncSession = Depends(get_session)) -> JobEnqueuedOut:
    await _load(session, server_id)
    await guard_update_scope(session, server_id)
    job_id = await job_manager.enqueue(MOD_UPDATE_APPLY_JOB_KIND, params={"scope": server_id})
    return JobEnqueuedOut(job_id=job_id, kind=MOD_UPDATE_APPLY_JOB_KIND)


@router.post("/{server_id}/mods/{guid}/pin", response_model=ServerOut, dependencies=authed)
async def pin_server_assignment(
    server_id: int, guid: str, body: ServerModPinIn, session: AsyncSession = Depends(get_session)
) -> Server:
    await _load(session, server_id)
    try:
        await pin_server_mod(session, server_id, guid, body.version, body.reason)
    except PinRecordNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except CurrentEngineBuildMissing as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await session.commit()
    return await _load(session, server_id)


@router.delete("/{server_id}/mods/{guid}/pin", response_model=ServerOut, dependencies=authed)
async def unpin_server_assignment(
    server_id: int, guid: str, session: AsyncSession = Depends(get_session)
) -> Server:
    await _load(session, server_id)
    try:
        await unpin_server_mod(session, server_id, guid)
    except PinRecordNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    await session.commit()
    return await _load(session, server_id)


@router.post("/{server_id}/rcon", dependencies=authed)
async def send_rcon_command(
    server_id: int, body: RconCommandIn, session: AsyncSession = Depends(get_session)
) -> dict:
    return {"response": await _rcon(await _load(session, server_id), body.command)}


@router.get("/{server_id}/players", dependencies=authed)
async def get_players(server_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    return {"players": await _rcon(await _load(session, server_id))}


@router.post("/{server_id}/schedule-restart", dependencies=authed)
async def schedule_server_restart(
    server_id: int, body: ScheduleRestartIn, session: AsyncSession = Depends(get_session)
) -> dict:
    server = await _load(session, server_id)
    if supervisor.active_server_id != server_id or not supervisor.is_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "that server is not the one running")
    if not server.rcon_enabled or not server.rcon_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "RCON is not configured for this server")
    try:
        return supervisor.schedule_restart(body.in_seconds, body.warn_at)
    except SupervisorError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("/{server_id}/schedule-restart", dependencies=authed)
async def get_server_restart_schedule(
    server_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    await _load(session, server_id)
    schedule = supervisor.get_restart_schedule()
    if schedule.get("armed") and supervisor.active_server_id != server_id:
        # The schedule belongs to the (single) other running definition.
        return {"armed": False, "restart_at": None, "warn_at": [], "seconds_remaining": None}
    return schedule


@router.delete("/{server_id}/schedule-restart", dependencies=authed)
async def cancel_server_restart(
    server_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    await _load(session, server_id)
    if supervisor.active_server_id == server_id:
        return supervisor.cancel_restart()
    return {"armed": False}


@router.get("/{server_id}/stats", dependencies=authed)
async def get_stats(server_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    return await _stats(await _load(session, server_id))


@router.get("/{server_id}/log", dependencies=authed)
async def get_log(
    server_id: int,
    severity: str | None = Query(default=None),
    hide_spam: bool = Query(default=True),
    q: str | None = Query(default=None),
    download: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    await _load(session, server_id)
    try:
        normalise_severity(severity)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if q is not None and not q.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "query must not be empty")
    if download:
        payload = read_log_download(server_id)
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "server log not found")
        return Response(
            payload.content,
            media_type=payload.content_type,
            headers={"Content-Disposition": f'attachment; filename="server-{server_id}-console.log"'},
        )
    log = current_log(server_id)
    if not log.exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "server log not found")
    if q is not None:
        result = search_log(server_id, q, severity=severity, hide_spam=hide_spam)
        lines, extra = result.matches, {"query": q, "scanned_bytes": result.scanned_bytes, "truncated": result.truncated}
    else:
        lines, extra = iter_log_lines(server_id, severity=severity, hide_spam=hide_spam), {}
    return {"server_id": server_id, "lines": [asdict(line) for line in lines], **extra}


@router.post("/{server_id}/start", dependencies=authed)
async def start_server(server_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    await _load(session, server_id)  # 404 check
    try:
        return await supervisor.start(server_id)
    except SingleServerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except (SupervisorError, LookupError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/{server_id}/stop", dependencies=authed)
async def stop_server(server_id: int) -> dict:
    if supervisor.active_server_id != server_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "that server is not the one running")
    try:
        return await supervisor.stop()
    except LookupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.websocket("/{server_id}/console")
async def server_console(
    websocket: WebSocket, server_id: int, token: str | None = Query(default=None),
    severity: str | None = Query(default=None), hide_spam: bool = Query(default=True),
) -> None:
    if not await ws_authenticate(websocket, token):
        return
    try:
        wanted = normalise_severity(severity)
    except ValueError:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    async with broadcaster.subscribe(server_console_channel(server_id)) as queue:
        try:
            while True:
                event = await queue.get()
                line = event.get("line") if isinstance(event, dict) else None
                if line is not None and ((wanted is not None and line_severity(line) != wanted) or (hide_spam and is_spam_line(line))):
                    continue
                await websocket.send_json(event)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return


@router.websocket("/{server_id}/stats")
async def server_stats(
    websocket: WebSocket, server_id: int, token: str | None = Query(default=None)
) -> None:
    if not await ws_authenticate(websocket, token):
        return
    await websocket.accept()
    try:
        async with SessionLocal() as session:
            server = await _load(session, server_id)
            while True:
                try:
                    await websocket.send_json(await _stats(server))
                except HTTPException as exc:
                    await websocket.send_json({"error": exc.detail, "status": exc.status_code})
                await asyncio.sleep(_STATS_INTERVAL_SECONDS)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
