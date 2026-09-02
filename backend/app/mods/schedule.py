"""Optional lifecycle-managed nightly engine and mod update checks.

This module deliberately does not create tasks at import time. FastAPI lifespan
code can create/start :class:`NightlyCheckScheduler` and stop it on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.config import settings
from ..core.db import SessionLocal
from ..steam.engine import refresh_engine
from .sync import run_mod_sync
from .updates import check_updates

logger = logging.getLogger("reforger.mods.schedule")


class _ScheduleContext:
    """Small in-memory substitute for the progress API used by ``run_mod_sync``."""

    def __init__(self) -> None:
        self.logs: list[str] = []
        self.progress_updates: list[dict[str, Any]] = []

    @property
    def cancelled(self) -> bool:
        return False

    async def log(self, line: str) -> None:
        self.logs.append(str(line))

    async def progress(self, pct: float, step: str | None = None) -> None:
        self.progress_updates.append({"pct": pct, "step": step})


def _engine_summary(engine: Any) -> dict[str, Any]:
    return {
        "installed_build": engine.installed_build,
        "latest_build": engine.latest_build,
        "update_available": engine.update_available,
    }


class NightlyCheckScheduler:
    """Run one non-overlapping check at a configured local time, then periodically."""

    def __init__(
        self,
        *,
        enabled: bool = settings.nightly_check_enabled,
        hour: int = settings.nightly_check_hour,
        interval_seconds: int = settings.nightly_check_interval_seconds,
        timezone: str = settings.tz,
        session_factory=SessionLocal,
        refresh_engine_fn: Callable[[Any], Awaitable[Any]] = refresh_engine,
        check_updates_fn: Callable[[str], Awaitable[dict[str, Any]]] = check_updates,
        mod_sync_fn: Callable[[Any], Awaitable[dict[str, Any]]] = run_mod_sync,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 0 <= hour <= 23:
            raise ValueError("hour must be between 0 and 23")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.enabled = enabled
        self.hour = hour
        self.interval_seconds = interval_seconds
        self.timezone = timezone
        try:
            self._tz = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            # The local Windows test venv may not ship the IANA tzdata package.
            logger.warning("timezone %s is unavailable; scheduling in UTC", timezone)
            self._tz = UTC
        self._session_factory = session_factory
        self._refresh_engine = refresh_engine_fn
        self._check_updates = check_updates_fn
        self._mod_sync = mod_sync_fn
        self._sleep = sleep_fn
        self._task: asyncio.Task[None] | None = None
        self._cycle_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """Start the sleep loop when enabled. Returns whether a task was created."""
        if not self.enabled or self.running:
            return False
        self._task = asyncio.create_task(self._run(), name="nightly-mod-check")
        return True

    async def stop(self) -> None:
        """Cancel and await the sleep loop, including an in-flight cycle."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run_cycle(self) -> dict[str, Any]:
        """Run and log a single check cycle, skipping if another cycle is active."""
        started_at = datetime.now(self._tz).isoformat()
        if self._cycle_lock.locked():
            summary = {
                "status": "skipped",
                "reason": "cycle_already_running",
                "started_at": started_at,
            }
            logger.info("nightly check summary: %s", json.dumps(summary, sort_keys=True))
            return summary

        async with self._cycle_lock:
            context = _ScheduleContext()
            try:
                async with self._session_factory() as session:
                    engine = await self._refresh_engine(session)
                updates = await self._check_updates("all")
                mod_sync = await self._mod_sync(context)
            except asyncio.CancelledError:
                logger.info(
                    "nightly check summary: %s",
                    json.dumps({"status": "cancelled", "started_at": started_at}, sort_keys=True),
                )
                raise
            except Exception as exc:
                summary = {
                    "status": "failed",
                    "started_at": started_at,
                    "finished_at": datetime.now(self._tz).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                logger.exception("nightly check summary: %s", json.dumps(summary, sort_keys=True))
                return summary

            summary = {
                "status": "completed",
                "started_at": started_at,
                "finished_at": datetime.now(self._tz).isoformat(),
                "engine": _engine_summary(engine),
                "updates": updates,
                "mod_sync": mod_sync,
            }
            logger.info("nightly check summary: %s", json.dumps(summary, default=str, sort_keys=True))
            return summary

    async def _run(self) -> None:
        await self._sleep(self._seconds_until_next_run())
        while True:
            await self.run_cycle()
            await self._sleep(self.interval_seconds)

    def _seconds_until_next_run(self) -> float:
        now = datetime.now(self._tz)
        scheduled = datetime.combine(now.date(), time(self.hour), tzinfo=now.tzinfo)
        if scheduled <= now:
            scheduled += timedelta(days=1)
        return (scheduled - now).total_seconds()
