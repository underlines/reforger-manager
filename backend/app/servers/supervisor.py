"""Process supervisor.

Enforces the **single running server** rule: ``start()`` refuses (raises
``SingleServerError``, surfaced as HTTP 409) if any server is running — checked
against both an in-memory handle and ``servers.is_running`` in the DB.

Spawns ``<SERVER_DIR>/ArmaReforgerServer`` with the Phase-2 launch args, tails
its console log into the events broadcaster, and watches for exit -> crash
detection. Graceful stop is SIGTERM then SIGKILL after a grace period.

Known crash-pattern matching is delegated to ``servers.diagnosis``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..core.config import settings
from ..core.db import SessionLocal
from ..core.events import broadcaster, server_console_channel
from ..models import Server, ServerConfigRevision
from ..rcon.client import RconClient
from ..steam.engine import scrape_display_version
from .config_gen import build_config, resolved_mod_entries, write_config
from .diagnosis import Diagnosis, diagnose

logger = logging.getLogger("reforger.supervisor")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _loopback(address: str) -> str:
    return "127.0.0.1" if address in {"", "0.0.0.0", "::"} else address


class SingleServerError(RuntimeError):
    """Raised when a start is attempted while another server is running."""


class SupervisorError(RuntimeError):
    pass


@dataclass
class ActiveServer:
    server_id: int
    process: asyncio.subprocess.Process
    log_path: Path
    config_path: Path
    started_at: datetime
    stop_requested: bool = False
    tail_task: asyncio.Task | None = field(default=None)
    wait_task: asyncio.Task | None = field(default=None)


class Supervisor:
    def __init__(self, session_factory=SessionLocal) -> None:
        self._sf = session_factory
        self._active: ActiveServer | None = None
        self._lock = asyncio.Lock()
        # In-memory scheduled restart: at most one, for the single running
        # server. Lost on backend restart by design (S16).
        self._restart_task: asyncio.Task | None = None
        self._restart_server_id: int | None = None
        self._restart_deadline: float | None = None  # loop-time seconds
        self._restart_at: datetime | None = None  # wall clock, for the API
        self._restart_warn_at: list[int] = []

    # ------------------------------------------------------------------ status
    @property
    def active_server_id(self) -> int | None:
        return self._active.server_id if self._active else None

    def is_running(self) -> bool:
        return self._active is not None and self._active.process.returncode is None

    def status(self) -> dict:
        a = self._active
        if a is None:
            return {"running": False, "server_id": None}
        return {
            "running": a.process.returncode is None,
            "server_id": a.server_id,
            "pid": a.process.pid,
            "started_at": a.started_at.isoformat(),
            "log_path": str(a.log_path),
            "stop_requested": a.stop_requested,
        }

    async def reconcile_on_startup(self) -> None:
        """A backend restart orphans any child processes. Clear stale flags."""
        async with self._sf() as session:
            rows = (
                await session.execute(select(Server).where(Server.is_running.is_(True)))
            ).scalars().all()
            for row in rows:
                row.is_running = False
                row.pid = None
                row.last_state = "exited"
                row.last_stopped_at = _utcnow()
            if rows:
                await session.commit()
                logger.info("cleared is_running on %d server row(s) after restart", len(rows))

    # ------------------------------------------------------------------- start
    async def start(self, server_id: int) -> dict:
        async with self._lock:
            if self.is_running():
                raise SingleServerError(
                    f"server {self._active.server_id} is already running"
                )

            async with self._sf() as session:
                other = (
                    await session.execute(
                        select(Server.id).where(
                            Server.is_running.is_(True), Server.id != server_id
                        )
                    )
                ).scalars().first()
                if other is not None:
                    raise SingleServerError(f"server {other} is already running")

                server = (
                    await session.execute(
                        select(Server)
                        .where(Server.id == server_id)
                        .options(selectinload(Server.mods))
                    )
                ).scalar_one_or_none()
                if server is None:
                    raise LookupError(f"server {server_id} not found")

                entries = await resolved_mod_entries(session, server)
                config = build_config(server, entries)
                config_path = write_config(server_id, config)

                server.config = config
                server.config_revision = (server.config_revision or 0) + 1
                session.add(
                    ServerConfigRevision(
                        server_id=server_id,
                        revision=server.config_revision,
                        snapshot=config,
                        note="auto-generated at start",
                        created_by="supervisor",
                    )
                )
                await session.commit()

            binary = settings.reforger_binary
            if not binary.exists():
                raise SupervisorError(
                    f"Reforger binary not found at {binary} (engine not installed?)"
                )

            profile_dir = settings.profile_dir(server_id)
            log_dir = profile_dir / "logs"
            addon_tmp = profile_dir / "addons_tmp"
            for directory in (profile_dir, log_dir, addon_tmp):
                directory.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "console.log"

            args = [
                str(binary),
                "-config", str(config_path),
                "-profile", str(profile_dir),
                # The engine reads/writes addons under ``<addonDownloadDir>/addons/``.
                # The migrated cache (and the scanner's ``addons_root()``) live at
                # ``<mods_dir>/reforger/addons/``, so the download dir must include
                # the ``reforger`` segment — otherwise the engine sees an empty
                # ``<mods_dir>/addons/`` and re-downloads every mod on each start.
                "-addonDownloadDir", str(settings.mods_dir / "reforger"),
                "-addonTempDir", str(addon_tmp),
                "-logStats", "30000",
                "-nothrow",
                "-maxFPS", "60",
                "-logLevel", "normal",
            ]
            logger.info("starting server %s: %s", server_id, " ".join(args))
            log_fh = open(log_path, "wb", buffering=0)
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(settings.server_dir),
                stdout=log_fh,
                stderr=asyncio.subprocess.STDOUT,
            )

            active = ActiveServer(
                server_id=server_id,
                process=process,
                log_path=log_path,
                config_path=config_path,
                started_at=_utcnow(),
            )
            self._active = active
            active.tail_task = asyncio.create_task(self._tail(active))
            active.wait_task = asyncio.create_task(self._watch_exit(active))

            async with self._sf() as session:
                server = await session.get(Server, server_id)
                if server is not None:
                    server.is_running = True
                    server.pid = process.pid
                    server.last_state = "running"
                    server.last_started_at = active.started_at
                    server.last_exit_code = None
                    await session.commit()

            await broadcaster.publish(
                server_console_channel(server_id),
                {"server_id": server_id, "event": "start", "pid": process.pid},
            )
            return {
                "server_id": server_id,
                "pid": process.pid,
                "config_path": str(config_path),
                "log_path": str(log_path),
            }

    # -------------------------------------------------------------------- stop
    async def stop(self, grace: float | None = None) -> dict:
        active = self._active
        if active is None:
            raise LookupError("no server is running")
        grace = settings.stop_grace_seconds if grace is None else grace
        active.stop_requested = True
        process = active.process

        logger.info("stopping server %s (pid %s), grace %.0fs", active.server_id, process.pid, grace)
        _terminate(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except asyncio.TimeoutError:
            logger.warning("server %s did not exit in %.0fs, sending SIGKILL", active.server_id, grace)
            process.kill()
            await process.wait()
        self._drop_restart_schedule_for(active.server_id)

        return {"server_id": active.server_id, "exit_code": process.returncode}

    # -------------------------------------------------------- scheduled restart
    async def _rcon_send(self, server_id: int, command: str) -> str:
        """Send one whitelisted RCON command to the running server's engine.

        Mirrors ``api/servers._rcon``: connect to the loopback address of the
        definition's RCON endpoint. Raises on any failure so callers can log
        and carry on (a missed warning must not abort the schedule).
        """
        async with self._sf() as session:
            server = await session.get(Server, server_id)
            if server is None:
                raise SupervisorError(f"server {server_id} not found")
            enabled = bool(server.rcon_enabled and server.rcon_password)
            host = _loopback(server.rcon_address)
            port = server.rcon_port
            password = server.rcon_password
        if not enabled:
            raise SupervisorError("RCON is not configured for this server")
        async with RconClient() as client:
            await client.connect(host, port, password)
            return await client.command(command)

    def schedule_restart(self, in_seconds: int, warn_at: list[int]) -> dict:
        """Arm one cancellable restart for the active server (replaces any prior).

        ``warn_at`` holds seconds-before-restart offsets (e.g. ``[300, 60, 10]``);
        a ``#say`` warning is sent at each, then ``#restart`` at T-0. The active
        server is marked ``stop_requested`` *before* the engine terminates
        itself so ``_watch_exit`` records an intentional stop, not a crash.
        """
        active = self._active
        if active is None or active.process.returncode is not None:
            raise SupervisorError("no server is running")
        self._cancel_restart_task()
        loop = asyncio.get_running_loop()
        offsets = sorted({int(w) for w in warn_at}, reverse=True)
        deadline = loop.time() + in_seconds
        self._restart_server_id = active.server_id
        self._restart_deadline = deadline
        self._restart_at = _utcnow() + timedelta(seconds=in_seconds)
        self._restart_warn_at = offsets
        task = asyncio.create_task(
            self._run_restart_schedule(active, deadline, offsets),
            name=f"restart-schedule-server-{active.server_id}",
        )
        self._restart_task = task
        task.add_done_callback(self._log_restart_task_failure)
        return self.get_restart_schedule()

    async def _run_restart_schedule(
        self, active: ActiveServer, deadline: float, offsets: list[int]
    ) -> None:
        server_id = active.server_id
        try:
            loop = asyncio.get_running_loop()
            for offset in offsets:  # descending: earliest absolute time first
                delay = deadline - offset - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if self._active is not active or active.process.returncode is not None:
                    return  # server went away; stop()/_watch_exit dropped the schedule
                try:
                    await self._rcon_send(server_id, f"#say Server restart in {offset} seconds")
                except Exception:
                    logger.warning(
                        "restart warning (-%ss) for server %s failed to send",
                        offset,
                        server_id,
                        exc_info=True,
                    )
            if self._active is not active or active.process.returncode is not None:
                return
            remaining = deadline - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            if self._active is not active or active.process.returncode is not None:
                return
            active.stop_requested = True  # engine's ensuing exit is intentional
            try:
                await self._rcon_send(server_id, "#restart")
            except Exception:
                # The command is already on the wire; the engine exits regardless.
                logger.warning("RCON #restart for server %s did not confirm", server_id, exc_info=True)
        except asyncio.CancelledError:
            logger.info("restart schedule for server %s cancelled", server_id)
            raise
        finally:
            self._clear_restart_schedule(asyncio.current_task())

    def get_restart_schedule(self) -> dict:
        """Report the schedule; ``armed: false`` whenever it is stale or gone."""
        task = self._restart_task
        active = self._active
        armed = bool(
            task is not None
            and not task.done()
            and active is not None
            and active.server_id == self._restart_server_id
            and active.process.returncode is None
        )
        if not armed or self._restart_deadline is None:
            return {"armed": False, "restart_at": None, "warn_at": [], "seconds_remaining": None}
        remaining = max(0, round(self._restart_deadline - asyncio.get_running_loop().time()))
        return {
            "armed": True,
            "restart_at": self._restart_at.isoformat() if self._restart_at else None,
            "warn_at": list(self._restart_warn_at),
            "seconds_remaining": remaining,
        }

    def cancel_restart(self) -> dict:
        self._cancel_restart_task()
        return {"armed": False}

    def _cancel_restart_task(self) -> None:
        task, self._restart_task = self._restart_task, None
        self._restart_server_id = None
        self._restart_deadline = None
        self._restart_at = None
        self._restart_warn_at = []
        if task is not None and not task.done():
            task.cancel()

    def _clear_restart_schedule(self, task: asyncio.Task | None = None) -> None:
        """Self-cleanup from the task's ``finally``; never clobbers a newer schedule."""
        if task is not None and self._restart_task is not task:
            return
        self._restart_task = None
        self._restart_server_id = None
        self._restart_deadline = None
        self._restart_at = None
        self._restart_warn_at = []

    def _drop_restart_schedule_for(self, server_id: int) -> None:
        """Drop the schedule when its server stops, for any reason."""
        if self._restart_server_id == server_id:
            self._cancel_restart_task()

    @staticmethod
    def _log_restart_task_failure(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("restart schedule task failed", exc_info=task.exception())

    # ---------------------------------------------------------------- internal
    async def _tail(self, active: ActiveServer) -> None:
        channel = server_console_channel(active.server_id)
        path = active.log_path
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.1)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                while True:
                    line = fh.readline()
                    if line:
                        await broadcaster.publish(
                            channel,
                            {"server_id": active.server_id, "line": line.rstrip("\n")},
                        )
                        continue
                    if active.process.returncode is not None:
                        for rest in fh.read().splitlines():
                            await broadcaster.publish(
                                channel,
                                {"server_id": active.server_id, "line": rest},
                            )
                        return
                    await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("log tail failed for server %s", active.server_id)

    async def _watch_exit(self, active: ActiveServer) -> None:
        rc = await active.process.wait()
        self._drop_restart_schedule_for(active.server_id)
        await asyncio.sleep(0.5)  # let the tailer drain the file
        if active.tail_task and not active.tail_task.done():
            active.tail_task.cancel()

        crashed = rc != 0 and not active.stop_requested
        if active.stop_requested:
            state = "stopped"
        elif crashed:
            state = "crashed"
        else:
            state = "exited"
        diagnosis = self.diagnose(active.log_path) if crashed else None
        display_version = scrape_display_version(active.log_path)

        async with self._sf() as session:
            server = await session.get(Server, active.server_id)
            if server is not None:
                server.is_running = False
                server.pid = None
                server.last_state = state
                server.last_exit_code = rc
                server.last_stopped_at = _utcnow()
                server.last_diagnosis = diagnosis.as_dict() if diagnosis else None
                await session.commit()
            if display_version:
                try:
                    from ..steam.engine import get_or_create_engine

                    engine_row = await get_or_create_engine(session)
                    engine_row.installed_version = display_version
                    await session.commit()
                except Exception:  # pragma: no cover
                    logger.debug("could not persist scraped display version", exc_info=True)

        logger.info("server %s exited rc=%s state=%s", active.server_id, rc, state)
        await broadcaster.publish(
            server_console_channel(active.server_id),
            {
                "server_id": active.server_id,
                "event": "exit",
                "exit_code": rc,
                "state": state,
                "diagnosis": diagnosis.as_dict() if diagnosis else None,
            },
        )
        if self._active is active:
            self._active = None

    def diagnose(self, log_path: str | Path) -> Diagnosis | None:
        return diagnose(log_path)


def _terminate(process: asyncio.subprocess.Process) -> None:
    try:
        process.send_signal(signal.SIGTERM)
    except (ProcessLookupError, ValueError, NotImplementedError, AttributeError):
        try:
            process.terminate()
        except ProcessLookupError:
            pass


# Process-wide singleton.
supervisor = Supervisor()
