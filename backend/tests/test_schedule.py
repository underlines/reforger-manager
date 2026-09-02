"""Deterministic checks for the optional nightly scheduler."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.mods.schedule import NightlyCheckScheduler


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_scheduler_does_not_create_task(self) -> None:
        scheduler = NightlyCheckScheduler(enabled=False)
        self.assertFalse(await scheduler.start())
        self.assertFalse(scheduler.running)

    async def test_cycle_returns_combined_summary(self) -> None:
        calls: list[str] = []

        async def refresh(session):
            calls.append("engine")
            return SimpleNamespace(installed_build="1", latest_build="2", update_available=True)

        async def updates(scope):
            calls.append(f"updates:{scope}")
            return {"checked": 3}

        async def sync(ctx):
            calls.append("sync")
            await ctx.progress(100, "done")
            return {"scanned": 3}

        scheduler = NightlyCheckScheduler(
            enabled=True,
            session_factory=_Session,
            refresh_engine_fn=refresh,
            check_updates_fn=updates,
            mod_sync_fn=sync,
        )
        summary = await scheduler.run_cycle()

        self.assertEqual(calls, ["engine", "updates:all", "sync"])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["engine"]["latest_build"], "2")
        self.assertEqual(summary["updates"], {"checked": 3})
        self.assertEqual(summary["mod_sync"], {"scanned": 3})
        self.assertIn("started_at", summary)
        self.assertIn("finished_at", summary)

    async def test_cycle_does_not_overlap(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def refresh(session):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return SimpleNamespace(installed_build="1", latest_build="1", update_available=False)

        async def unused(*_args):
            return {}

        scheduler = NightlyCheckScheduler(
            session_factory=_Session,
            refresh_engine_fn=refresh,
            check_updates_fn=unused,
            mod_sync_fn=unused,
        )
        first = asyncio.create_task(scheduler.run_cycle())
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = await scheduler.run_cycle()
        release.set()
        await first

        self.assertEqual(calls, 1)
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "cycle_already_running")

    async def test_stop_cancels_sleep_loop(self) -> None:
        sleeping = asyncio.Event()

        async def sleep(_seconds):
            sleeping.set()
            await asyncio.Event().wait()

        scheduler = NightlyCheckScheduler(enabled=True, sleep_fn=sleep)
        self.assertTrue(await scheduler.start())
        await sleeping.wait()
        await scheduler.stop()
        self.assertFalse(scheduler.running)


if __name__ == "__main__":
    unittest.main()
