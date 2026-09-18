"""Story S4: ``start_server`` surfaces blocking pre-flight findings.

Self-contained: a real sqlite session backs ``_load``; the supervisor and
``preflight`` are patched at the exact seam the route uses
(``app.api.servers.supervisor`` / ``app.api.servers.preflight``) so no process
is spawned and no Workshop is queried. The route is invoked directly, which is
the same coroutine FastAPI dispatches.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import servers as servers_api
from app.core.db import Base
from app.models import Server
from app.servers.preflight import PreflightCheck, PreflightReport


def _check(name: str, level: str, detail: str) -> PreflightCheck:
    return PreflightCheck(name, level, detail)


class StartServerPreflightFindingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.sessions() as session:
            server = Server(name="main")
            session.add(server)
            await session.commit()
            self.server_id = server.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _start(self, report: PreflightReport) -> tuple[dict, AsyncMock, AsyncMock]:
        payload = {"server_id": self.server_id, "pid": 4242, "config_path": "c", "log_path": "l"}
        start = AsyncMock(return_value=dict(payload))
        preflight = AsyncMock(return_value=report)
        with patch.object(servers_api, "supervisor", SimpleNamespace(start=start)), patch.object(
            servers_api, "preflight", preflight
        ):
            async with self.sessions() as session:
                result = await servers_api.start_server(self.server_id, session)
        return result, start, preflight

    async def test_blocked_findings_are_surfaced_and_start_still_succeeds(self) -> None:
        blocked = _check("Workshop", "blocked", "addon missing")
        report = PreflightReport(
            "blocked",
            [blocked, _check("preflight", "green", "all good"), _check("scenario", "warn", "stale")],
            [],
        )
        result, start, preflight = await self._start(report)

        # The start itself still happened and the existing payload survives.
        self.assertEqual(result["server_id"], self.server_id)
        self.assertEqual(result["pid"], 4242)
        # Only the blocking finding is surfaced, in the get_preflight shape.
        self.assertEqual(result["preflight_blocked"], [blocked.as_dict()])
        start.assert_awaited_once_with(self.server_id)
        preflight.assert_awaited_once()

    async def test_clean_server_reports_an_empty_blocked_list(self) -> None:
        report = PreflightReport("green", [_check("preflight", "green", "all good")], [])
        result, _, _ = await self._start(report)

        self.assertEqual(result["preflight_blocked"], [])
        self.assertIn("preflight_blocked", result)
