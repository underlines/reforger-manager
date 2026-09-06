"""Bans API + raw-RCON route tests (S8).

No real game process and no real RCON traffic: the active server is a stub
``ActiveServer`` and ``RconClient`` is replaced with an AsyncMock whose
``bans``/``ban_create``/``ban_remove``/``command``/``_command`` record calls.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.servers.supervisor import ActiveServer


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.pid = 4242

    def send_signal(self, _sig: object) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0

    async def wait(self) -> int | None:
        return self.returncode


def fake_active(server_id: int = 1) -> ActiveServer:
    return ActiveServer(
        server_id=server_id,
        process=FakeProcess(),
        log_path=Path("console.log"),
        config_path=Path("config.json"),
        started_at=datetime.now(timezone.utc),
    )


BAN_ROWS = [
    {"ban_id": "0", "uid": "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809", "duration": "3600",
     "raw": "0 ; 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809 ; 3600"}
]


class BansRouteTests(unittest.IsolatedAsyncioTestCase):
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

        self.servers_api = servers_api
        self._client = MagicMock()
        self._client.__aenter__.return_value = self._client
        self._client.connect = AsyncMock()
        self._client.bans = AsyncMock(return_value=(BAN_ROWS, "raw bans text"))
        self._client.ban_create = AsyncMock(return_value="Ban created")
        self._client.ban_remove = AsyncMock(return_value="Ban removed")
        self._client.command = AsyncMock(return_value="ok")
        self._client._command = AsyncMock(return_value="raw ok")

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        self.servers_api.supervisor._active = None
        self.servers_api.supervisor.__dict__.pop("_rcon_send", None)
        await self.engine.dispose()

    async def _create_server(self, name: str, rcon_password: str | None = None,
                             rcon_permission: str | None = None) -> int:
        response = await self.client.post("/api/servers", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        server_id = response.json()["id"]
        patch: dict = {}
        if rcon_password is not None:
            patch["rcon_password"] = rcon_password
        if rcon_permission is not None:
            patch["rcon_permission"] = rcon_permission
        if patch:
            response = await self.client.patch(f"/api/servers/{server_id}", json=patch)
            self.assertEqual(response.status_code, 200, response.text)
        return server_id

    def _go_live(self, server_id: int) -> None:
        self.servers_api.supervisor._active = fake_active(server_id)
        self._patch = patch.object(
            self.servers_api, "RconClient", return_value=self._client
        )
        self._sup = patch.object(
            self.servers_api,
            "supervisor",
            SimpleNamespace(
                active_server_id=server_id,
                is_running=lambda: True,
                _active=self.servers_api.supervisor._active,
            ),
        )
        self._patch.start()
        self._sup.start()

    def _teardown_live(self) -> None:
        self._sup.stop()
        self._patch.stop()

    # ----------------------------------------------------------------- tests
    async def test_get_bans_happy_path(self) -> None:
        server_id = await self._create_server("Live", rcon_password="secret")
        self._go_live(server_id)
        try:
            response = await self.client.get(f"/api/servers/{server_id}/bans?page=1")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["bans"], BAN_ROWS)
            self.assertEqual(response.json()["raw"], "raw bans text")
            self.assertEqual(response.json()["page"], 1)
            self._client.bans.assert_awaited_once_with(1)
        finally:
            self._teardown_live()

    async def test_post_bans_happy_path(self) -> None:
        server_id = await self._create_server("Live", rcon_password="secret")
        self._go_live(server_id)
        try:
            response = await self.client.post(
                f"/api/servers/{server_id}/bans",
                json={"identifier": "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809",
                      "duration_seconds": 3600, "reason": "spam"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"echo": "Ban created"})
            self._client.ban_create.assert_awaited_once_with(
                "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809", 3600, "spam"
            )
        finally:
            self._teardown_live()

    async def test_delete_bans_happy_path(self) -> None:
        server_id = await self._create_server("Live", rcon_password="secret")
        self._go_live(server_id)
        try:
            response = await self.client.delete(
                f"/api/servers/{server_id}/bans/1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809"
            )
            self.assertEqual(response.status_code, 204, response.text)
            self.assertEqual(response.content, b"")
            self._client.ban_remove.assert_awaited_once_with(
                "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809"
            )
        finally:
            self._teardown_live()

    async def test_post_rcon_raw_skips_validation_but_keeps_guard(self) -> None:
        server_id = await self._create_server("Live", rcon_password="secret")
        self._go_live(server_id)
        try:
            response = await self.client.post(
                f"/api/servers/{server_id}/rcon?raw=1", json={"command": "#lock session"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"response": "raw ok"})
            self._client._command.assert_awaited_once_with("#lock session")
            self._client.command.assert_not_awaited()
        finally:
            self._teardown_live()

    async def test_rcon_default_still_validates(self) -> None:
        server_id = await self._create_server("Live", rcon_password="secret")
        self._go_live(server_id)
        try:
            response = await self.client.post(
                f"/api/servers/{server_id}/rcon", json={"command": "#players"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self._client.command.assert_awaited_once_with("#players")
            self._client._command.assert_not_awaited()
        finally:
            self._teardown_live()

    async def test_bans_returns_400_when_rcon_disabled(self) -> None:
        server_id = await self._create_server("NoRcon")
        self._go_live(server_id)
        try:
            response = await self.client.get(f"/api/servers/{server_id}/bans")
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("RCON is not configured", response.text)
        finally:
            self._teardown_live()

    async def test_bans_returns_409_when_not_the_running_server(self) -> None:
        server_id = await self._create_server("Idle", rcon_password="secret")
        response = await self.client.get(f"/api/servers/{server_id}/bans")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_monitor_rejects_ban_list(self) -> None:
        server_id = await self._create_server("Monitor", rcon_password="secret",
                                              rcon_permission="monitor")
        self._go_live(server_id)
        try:
            response = await self.client.get(f"/api/servers/{server_id}/bans")
            self.assertEqual(response.status_code, 403, response.text)
            self._client.bans.assert_not_awaited()
        finally:
            self._teardown_live()

    async def test_monitor_rejects_raw_command_other_than_players(self) -> None:
        server_id = await self._create_server("Monitor", rcon_password="secret",
                                              rcon_permission="monitor")
        self._go_live(server_id)
        try:
            response = await self.client.post(
                f"/api/servers/{server_id}/rcon?raw=1", json={"command": "#lock"}
            )
            self.assertEqual(response.status_code, 403, response.text)
            self._client._command.assert_not_awaited()
        finally:
            self._teardown_live()


if __name__ == "__main__":
    unittest.main()