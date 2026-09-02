"""Scheduled restart: supervisor timer + /schedule-restart routes (S16).

No real game process and no real RCON traffic: the active server is a stub
``ActiveServer`` and ``Supervisor._rcon_send`` is replaced with a recorder.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.servers.supervisor import ActiveServer, Supervisor, SupervisorError


class FakeProcess:
    """Just enough of asyncio.subprocess.Process for is_running()/stop()."""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.pid = 4242

    def send_signal(self, _sig: object) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0

    async def wait(self) -> int | None:
        return self.returncode


def fake_active(server_id: int = 1, returncode: int | None = None) -> ActiveServer:
    return ActiveServer(
        server_id=server_id,
        process=FakeProcess(returncode),
        log_path=Path("console.log"),
        config_path=Path("config.json"),
        started_at=datetime.now(timezone.utc),
    )


IDLE_SCHEDULE = {"armed": False, "restart_at": None, "warn_at": [], "seconds_remaining": None}


class SupervisorScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.sup = Supervisor()
        self.sup._active = fake_active(1)
        self.sent: list[tuple[float, str]] = []
        self.sup._rcon_send = self._record

    async def _record(self, server_id: int, command: str) -> str:
        self.sent.append((asyncio.get_running_loop().time(), command))
        return "ok"

    # ----------------------------------------------------------------- tests
    async def test_warnings_fire_in_order_then_restart(self) -> None:
        """warn_at=[2,1] over in_seconds=2: #say now, #say at T-1, #restart at T-0."""
        result = self.sup.schedule_restart(in_seconds=2, warn_at=[2, 1])
        self.assertTrue(result["armed"])
        self.assertEqual(result["warn_at"], [2, 1])

        await asyncio.sleep(2.4)

        self.assertEqual(
            [command for _t, command in self.sent],
            [
                "#say Server restart in 2 seconds",
                "#say Server restart in 1 seconds",
                "#restart",
            ],
        )
        times = [t for t, _command in self.sent]
        self.assertLess(times[0], times[1])
        self.assertLess(times[1], times[2])
        # The engine's ensuing exit must be classified as an intentional stop.
        self.assertTrue(self.sup._active.stop_requested)
        self.assertEqual(self.sup.get_restart_schedule(), IDLE_SCHEDULE)

    async def test_failed_warning_does_not_abort_restart(self) -> None:
        async def flaky(_server_id: int, command: str) -> str:
            self.sent.append((asyncio.get_running_loop().time(), command))
            if command.startswith("#say"):
                raise RuntimeError("rcon down")
            return "ok"

        self.sup._rcon_send = flaky
        self.sup.schedule_restart(in_seconds=1, warn_at=[1])

        await asyncio.sleep(1.3)

        self.assertEqual(
            [command for _t, command in self.sent],
            ["#say Server restart in 1 seconds", "#restart"],
        )

    async def test_cancel_stops_pending_warnings(self) -> None:
        self.sup.schedule_restart(in_seconds=5, warn_at=[3])
        await asyncio.sleep(0.2)

        self.assertEqual(self.sent, [])
        self.assertEqual(self.sup.cancel_restart(), {"armed": False})
        self.assertEqual(self.sup.get_restart_schedule(), IDLE_SCHEDULE)

        await asyncio.sleep(0.1)  # nothing further may fire
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.sup._restart_task)

    async def test_get_reports_armed_with_remaining(self) -> None:
        result = self.sup.schedule_restart(in_seconds=30, warn_at=[10])
        self.assertTrue(result["armed"])
        self.assertEqual(result["warn_at"], [10])
        self.assertTrue(29 <= result["seconds_remaining"] <= 30)
        self.assertIsNotNone(result["restart_at"])
        self.sup.cancel_restart()

    async def test_armed_false_after_task_completes(self) -> None:
        self.sup.schedule_restart(in_seconds=0, warn_at=[])
        await asyncio.sleep(0.3)
        self.assertEqual([command for _t, command in self.sent], ["#restart"])
        self.assertEqual(self.sup.get_restart_schedule(), IDLE_SCHEDULE)

    async def test_schedule_dropped_when_server_stops(self) -> None:
        """stop() hooks the same drop the exit watcher uses."""
        self.sup.schedule_restart(in_seconds=30, warn_at=[10])
        result = await self.sup.stop()
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.sup.get_restart_schedule(), IDLE_SCHEDULE)
        self.assertIsNone(self.sup._restart_task)

    async def test_schedule_requires_running_server(self) -> None:
        sup = Supervisor()
        with self.assertRaises(SupervisorError):
            sup.schedule_restart(10, [5])

    async def test_new_schedule_replaces_and_survives_old_task_cleanup(self) -> None:
        old = self.sup.schedule_restart(in_seconds=30, warn_at=[25])
        self.assertTrue(old["armed"])
        new = self.sup.schedule_restart(in_seconds=30, warn_at=[10])
        self.assertEqual(new["warn_at"], [10])
        await asyncio.sleep(0.1)  # let the cancelled old task run its finally
        self.assertEqual(new["warn_at"], self.sup.get_restart_schedule()["warn_at"])
        self.sup.cancel_restart()


class ScheduleRestartRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.main import app

        self.app = app
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        self._overridden = (get_session, get_current_user)
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None

        from app.api import servers as servers_api

        self.sup = servers_api.supervisor
        self.sup._rcon_send = self._record
        self.sent: list[str] = []

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        self.sup._cancel_restart_task()
        self.sup._active = None
        self.sup.__dict__.pop("_rcon_send", None)
        await self.engine.dispose()

    async def _record(self, _server_id: int, command: str) -> str:
        self.sent.append(command)
        return "ok"

    async def _create_server(self, name: str, rcon_password: str | None = None) -> int:
        response = await self.client.post("/api/servers", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        server_id = response.json()["id"]
        if rcon_password is not None:
            response = await self.client.patch(
                f"/api/servers/{server_id}", json={"rcon_password": rcon_password}
            )
            self.assertEqual(response.status_code, 200, response.text)
        return server_id

    def _go_live(self, server_id: int) -> None:
        self.sup._active = fake_active(server_id)

    # ----------------------------------------------------------------- tests
    async def test_post_returns_409_when_not_the_running_server(self) -> None:
        server_id = await self._create_server("Idle", rcon_password="secret")
        response = await self.client.post(
            f"/api/servers/{server_id}/schedule-restart", json={"in_seconds": 60}
        )
        self.assertEqual(response.status_code, 409, response.text)

    async def test_post_returns_409_for_a_different_running_server(self) -> None:
        active_id = await self._create_server("Active", rcon_password="secret")
        other_id = await self._create_server("Other")
        self._go_live(active_id)
        response = await self.client.post(
            f"/api/servers/{other_id}/schedule-restart", json={"in_seconds": 60}
        )
        self.assertEqual(response.status_code, 409, response.text)

    async def test_post_returns_400_when_rcon_not_configured(self) -> None:
        server_id = await self._create_server("NoRcon")
        self._go_live(server_id)
        response = await self.client.post(
            f"/api/servers/{server_id}/schedule-restart", json={"in_seconds": 60}
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("RCON is not configured", response.text)

    async def test_post_get_delete_roundtrip(self) -> None:
        server_id = await self._create_server("Live", rcon_password="secret")
        self._go_live(server_id)

        response = await self.client.post(
            f"/api/servers/{server_id}/schedule-restart",
            json={"in_seconds": 120, "warn_at": [60, 30]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        armed = response.json()
        self.assertTrue(armed["armed"])
        self.assertEqual(armed["warn_at"], [60, 30])
        self.assertTrue(119 <= armed["seconds_remaining"] <= 120)
        self.assertIsNotNone(armed["restart_at"])

        response = await self.client.get(f"/api/servers/{server_id}/schedule-restart")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["armed"])

        response = await self.client.delete(f"/api/servers/{server_id}/schedule-restart")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"armed": False})

        response = await self.client.get(f"/api/servers/{server_id}/schedule-restart")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["armed"])
        self.assertEqual(self.sent, [])  # nothing fired at this timescale

    async def test_get_and_delete_report_idle_for_other_server(self) -> None:
        active_id = await self._create_server("Active", rcon_password="secret")
        other_id = await self._create_server("Bystander")
        self._go_live(active_id)

        response = await self.client.post(
            f"/api/servers/{active_id}/schedule-restart", json={"in_seconds": 60}
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = await self.client.get(f"/api/servers/{other_id}/schedule-restart")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["armed"])

        response = await self.client.delete(f"/api/servers/{other_id}/schedule-restart")
        self.assertEqual(response.json(), {"armed": False})

        response = await self.client.get(f"/api/servers/{active_id}/schedule-restart")
        self.assertTrue(response.json()["armed"])  # the bystander did not cancel it

        await self.client.delete(f"/api/servers/{active_id}/schedule-restart")

    async def test_unknown_server_is_404(self) -> None:
        for method, kwargs in (
            ("get", {}),
            ("post", {"json": {"in_seconds": 60}}),
            ("delete", {}),
        ):
            response = await getattr(self.client, method)(
                "/api/servers/9999/schedule-restart", **kwargs
            )
            self.assertEqual(response.status_code, 404, response.text)

    async def test_in_seconds_must_be_positive(self) -> None:
        server_id = await self._create_server("Live", rcon_password="secret")
        self._go_live(server_id)
        response = await self.client.post(
            f"/api/servers/{server_id}/schedule-restart", json={"in_seconds": 0}
        )
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
