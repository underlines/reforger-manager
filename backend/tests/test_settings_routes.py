"""Route-level checks for the runtime settings API (S17): GET/PATCH
/api/settings with the nightly scheduler singleton, POST /api/auth/password,
and the settings-driven spam filter behind is_spam_line.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.app_settings import invalidate_spam_cache
from app.core.config import settings as app_config
from app.core.db import Base, get_session
from app.core.security import get_current_user, hash_password, verify_password
from app.mods.logview import is_spam_line
from app.models import APP_SETTINGS_SINGLETON_ID, AppSettings, Server, User


class _RouteFixture:
    """Shared in-memory app fixture; concrete classes stay IsolatedAsyncioTestCase."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
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

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        invalidate_spam_cache()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        # Never leak warmed patterns into other test modules.
        invalidate_spam_cache()
        await self.engine.dispose()


class SpyScheduler:
    """Stands in for NightlyCheckScheduler so no real asyncio task is created."""

    instances: list["SpyScheduler"] = []

    def __init__(self, *, enabled: bool = False, hour: int = 3, **_kwargs) -> None:
        self.enabled = enabled
        self.hour = hour
        self.started = False
        self.stopped = False
        SpyScheduler.instances.append(self)

    async def start(self) -> bool:
        self.started = True
        return self.enabled

    async def stop(self) -> None:
        self.stopped = True

    @property
    def running(self) -> bool:
        return self.started and not self.stopped


class SettingsRouteTests(_RouteFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Settings routes never use the user object; unauthenticated is fine here.
        self.app.dependency_overrides[get_current_user] = lambda: None

    # ----------------------------------------------------------------- tests
    async def test_get_returns_env_fallback_defaults_and_seeds_row(self) -> None:
        response = await self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "nightly_check_enabled": app_config.nightly_check_enabled,
                "nightly_check_hour": app_config.nightly_check_hour,
                "log_spam_patterns": ["thermalprofiledefault.conf"],
            },
        )
        async with self.sessions() as session:
            row = await session.get(AppSettings, APP_SETTINGS_SINGLETON_ID)
            self.assertIsNotNone(row)
            self.assertEqual(row.nightly_check_enabled, app_config.nightly_check_enabled)
            self.assertEqual(row.nightly_check_hour, app_config.nightly_check_hour)
            self.assertEqual(row.log_spam_patterns, ["thermalprofiledefault.conf"])

    async def test_fresh_row_keeps_default_spam_pattern_case_insensitive(self) -> None:
        await self.client.get("/api/settings")  # seeds the singleton row

        self.assertTrue(is_spam_line("ENGINE (E): Failed loading thermalProfileDefault.conf"))
        self.assertTrue(is_spam_line("...THERMALPROFILEDEFAULT.CONF..."))
        self.assertFalse(is_spam_line("SCRIPT (I): all quiet"))

    async def test_cold_cache_falls_back_to_built_in_default(self) -> None:
        set_override = {"log_spam_patterns": ["only-this-one"]}
        self.assertEqual(
            (await self.client.patch("/api/settings", json=set_override)).status_code, 200
        )
        self.assertTrue(is_spam_line("ONLY-THIS-ONE anywhere"))
        self.assertFalse(is_spam_line("thermalProfileDefault.conf"))

        invalidate_spam_cache()  # cold again -> historical behaviour

        self.assertTrue(is_spam_line("ENGINE (E): Failed loading thermalProfileDefault.conf"))
        self.assertFalse(is_spam_line("ONLY-THIS-ONE anywhere"))

    async def test_patch_reflects_values_and_persists(self) -> None:
        response = await self.client.patch(
            "/api/settings",
            json={"nightly_check_enabled": True, "nightly_check_hour": 5},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["nightly_check_enabled"])
        self.assertEqual(response.json()["nightly_check_hour"], 5)
        async with self.sessions() as session:
            row = await session.get(AppSettings, APP_SETTINGS_SINGLETON_ID)
            self.assertTrue(row.nightly_check_enabled)
            self.assertEqual(row.nightly_check_hour, 5)

    async def test_patch_out_of_range_hour_is_422(self) -> None:
        response = await self.client.patch("/api/settings", json={"nightly_check_hour": 24})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            (await self.client.patch("/api/settings", json={"nightly_check_hour": -1})).status_code,
            422,
        )

    async def test_patch_toggle_starts_and_stops_scheduler_singleton(self) -> None:
        from app import main

        original = main.nightly_scheduler
        main.nightly_scheduler = None
        try:
            with patch.object(main, "NightlyCheckScheduler", SpyScheduler):
                SpyScheduler.instances = []
                response = await self.client.patch(
                    "/api/settings",
                    json={"nightly_check_enabled": True, "nightly_check_hour": 5},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(SpyScheduler.instances), 1)
                spy = SpyScheduler.instances[0]
                self.assertEqual((spy.enabled, spy.hour), (True, 5))
                self.assertIs(main.nightly_scheduler, spy)
                self.assertTrue(spy.running)

                response = await self.client.patch(
                    "/api/settings", json={"nightly_check_enabled": False}
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(spy.stopped)
                self.assertFalse(spy.running)
                self.assertIsNone(main.nightly_scheduler)
        finally:
            main.nightly_scheduler = original

    async def test_patch_hour_restarts_running_scheduler(self) -> None:
        from app import main

        original = main.nightly_scheduler
        main.nightly_scheduler = None
        try:
            with patch.object(main, "NightlyCheckScheduler", SpyScheduler):
                SpyScheduler.instances = []
                await self.client.patch(
                    "/api/settings", json={"nightly_check_enabled": True, "nightly_check_hour": 3}
                )
                response = await self.client.patch(
                    "/api/settings", json={"nightly_check_hour": 7}
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["nightly_check_hour"], 7)
                self.assertEqual(len(SpyScheduler.instances), 2)
                self.assertTrue(SpyScheduler.instances[0].stopped)
                self.assertEqual(SpyScheduler.instances[1].hour, 7)
                self.assertIs(main.nightly_scheduler, SpyScheduler.instances[1])
                self.assertTrue(SpyScheduler.instances[1].running)
        finally:
            main.nightly_scheduler = original

    async def test_patch_spam_patterns_filters_stored_log_and_cache(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profiles = Path(tmp.name)
        log_dir = profiles / "3" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "console.log").write_text(
            "ENGINE (I): Booting server\n"
            "SCRIPT (I): noisy warning xyzzy\n"
            "ENGINE (E): Failed loading thermalProfileDefault.conf\n",
            encoding="utf-8",
        )
        async with self.sessions() as session:
            session.add(Server(id=3, name="logtest"))
            await session.commit()

        with patch.object(app_config, "profiles_dir", profiles):
            before = (await self.client.get("/api/servers/3/log")).json()
            self.assertEqual(
                [(line["line_number"], line["is_spam"]) for line in before["lines"]],
                [(1, False), (2, False)],
            )

            response = await self.client.patch(
                "/api/settings",
                json={
                    "log_spam_patterns": ["thermalprofiledefault.conf", "noisy warning xyzzy"]
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["log_spam_patterns"],
                ["thermalprofiledefault.conf", "noisy warning xyzzy"],
            )

            # The in-process cache took effect (not just the DB row)...
            self.assertTrue(is_spam_line("SCRIPT (I): NOISY WARNING XYZZY"))
            # ...and the stored-log endpoint now filters the matching line.
            after = (await self.client.get("/api/servers/3/log")).json()
            self.assertEqual([line["line_number"] for line in after["lines"]], [1])
            shown = (await self.client.get("/api/servers/3/log?hide_spam=false")).json()
            self.assertEqual(
                [(line["line_number"], line["is_spam"]) for line in shown["lines"]],
                [(1, False), (2, True), (3, True)],
            )


class PasswordRouteTests(_RouteFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()

        async def fake_current_user(session: AsyncSession = Depends(get_session)) -> User | None:
            return (
                await session.execute(select(User).where(User.username == "admin"))
            ).scalar_one_or_none()

        self.app.dependency_overrides[get_current_user] = fake_current_user
        async with self.sessions() as session:
            session.add(
                User(username="admin", password_hash=hash_password("oldpassword"))
            )
            await session.commit()

    # ----------------------------------------------------------------- tests
    async def test_wrong_current_password_is_401(self) -> None:
        response = await self.client.post(
            "/api/auth/password",
            json={"current_password": "wrong", "new_password": "newpassword1"},
        )
        self.assertEqual(response.status_code, 401)
        # Nothing was changed.
        async with self.sessions() as session:
            user = (
                await session.execute(select(User).where(User.username == "admin"))
            ).scalar_one()
            self.assertTrue(verify_password("oldpassword", user.password_hash))

    async def test_too_short_new_password_is_422(self) -> None:
        response = await self.client.post(
            "/api/auth/password",
            json={"current_password": "oldpassword", "new_password": "short"},
        )
        self.assertEqual(response.status_code, 422)

    async def test_valid_change_rehashes_and_keeps_old_invalid(self) -> None:
        response = await self.client.post(
            "/api/auth/password",
            json={"current_password": "oldpassword", "new_password": "newpassword1"},
        )
        self.assertEqual(response.status_code, 200, response.text)

        async with self.sessions() as session:
            user = (
                await session.execute(select(User).where(User.username == "admin"))
            ).scalar_one()
            self.assertTrue(verify_password("newpassword1", user.password_hash))
            self.assertFalse(verify_password("oldpassword", user.password_hash))


if __name__ == "__main__":
    unittest.main()
