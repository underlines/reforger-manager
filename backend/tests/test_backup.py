"""Route-level checks for the full backup export / import API (S18)."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import Modpack, ModpackItem, Server, ServerMod

GUID_A = "AAAABBBBCCCCDDDD"
GUID_B = "1111222233334444"
GUID_C = "9999888877776666"

PINNED_AT = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)


class BackupRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.main import app
        from app.api import backup as backup_api

        self.app = app
        # The integrator wires this router into main.py ahead of the SPA
        # catch-all; the test app is a module singleton, so register once and
        # keep it in front of the "/{full_path:path}" fallback.
        if not any(getattr(r, "path", None) == "/api/backup/export" for r in app.routes):
            routes = app.router.routes
            spa = [r for r in routes if getattr(r, "path", None) == "/{full_path:path}"]
            for route in spa:
                routes.remove(route)
            app.include_router(backup_api.router, prefix="/api")
            routes.extend(spa)

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

        from app.servers.supervisor import supervisor

        self.supervisor = supervisor
        self.supervisor._active = None

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        self.supervisor._active = None
        await self.engine.dispose()

    # ---------------------------------------------------------------- helpers
    def _mark_running(self, server_id: int) -> None:
        self.supervisor._active = SimpleNamespace(
            server_id=server_id, process=SimpleNamespace(returncode=None)
        )

    async def _seed_baseline(self) -> None:
        """Two servers (one with a pinned mod set) + one modpack."""
        async with self.sessions() as session:
            alpha = Server(
                name="Alpha",
                game_password="alpha-game",
                admin_password="alpha-admin",
                rcon_password="alpha-rcon",
                rcon_enabled=True,
                max_players=48,
                is_favourite=True,
                scenario_game_id="{ABC}Missions/Alpha.conf",
                game_properties={"serverMaxViewDistance": 2500},
                extra_config={"a2s": {"address": "0.0.0.0"}},
            )
            alpha.mods.extend(
                [
                    ServerMod(
                        mod_guid=GUID_A,
                        mod_name="Mod A",
                        load_order=0,
                        enabled=True,
                        pinned_version="1.2.3",
                        pinned_at_build="0.9.8.123",
                        pinned_reason="stability",
                        pinned_at=PINNED_AT,
                    ),
                    ServerMod(
                        mod_guid=GUID_B,
                        mod_name="Mod B",
                        load_order=1,
                        enabled=False,
                    ),
                ]
            )
            bravo = Server(name="Bravo", game_password="bravo-game")
            bravo.mods.append(
                ServerMod(mod_guid=GUID_C, mod_name="Mod C", load_order=0, enabled=True)
            )
            pack = Modpack(name="Core", description="core pack")
            pack.items.extend(
                [
                    ModpackItem(mod_guid=GUID_A, load_order=0),
                    ModpackItem(mod_guid=GUID_C, load_order=1),
                ]
            )
            session.add_all([alpha, bravo, pack])
            await session.commit()

    async def _server_by_name(self, name: str) -> dict | None:
        rows = (await self.client.get("/api/servers")).json()
        return next((s for s in rows if s["name"] == name), None)

    # ----------------------------------------------------------------- tests
    async def test_export_import_round_trip(self) -> None:
        await self._seed_baseline()

        export = (await self.client.get("/api/backup/export")).json()
        self.assertEqual(export["version"], 1)
        self.assertIsNotNone(export["exported_at"])
        self.assertEqual({s["name"] for s in export["servers"]}, {"Alpha", "Bravo"})
        self.assertEqual([p["name"] for p in export["modpacks"]], ["Core"])
        alpha_dump = next(s for s in export["servers"] if s["name"] == "Alpha")
        self.assertEqual(alpha_dump["admin_password"], "alpha-admin")
        self.assertEqual(alpha_dump["rcon_password"], "alpha-rcon")
        self.assertEqual(alpha_dump["game_properties"], {"serverMaxViewDistance": 2500})
        self.assertEqual([m["mod_guid"] for m in alpha_dump["mods"]], [GUID_A, GUID_B])
        self.assertEqual(alpha_dump["mods"][0]["pinned_version"], "1.2.3")
        self.assertNotIn("id", alpha_dump)
        self.assertNotIn("is_running", alpha_dump)
        self.assertNotIn("config", alpha_dump)

        # Wipe everything the backup covers.
        for srv in (await self.client.get("/api/servers")).json():
            self.assertEqual((await self.client.delete(f"/api/servers/{srv['id']}")).status_code, 204)
        for pack in (await self.client.get("/api/modpacks")).json():
            self.assertEqual((await self.client.delete(f"/api/modpacks/{pack['id']}")).status_code, 204)
        self.assertEqual((await self.client.get("/api/servers")).json(), [])

        plan = (
            await self.client.post("/api/backup/import?dry_run=false", json=export)
        ).json()
        self.assertEqual(
            sorted((p["name"], p["action"]) for p in plan["servers"]),
            [("Alpha", "create"), ("Bravo", "create")],
        )
        self.assertEqual(plan["modpacks"], [{"name": "Core", "action": "create", "note": None}])

        alpha = await self._server_by_name("Alpha")
        self.assertEqual(alpha["admin_password"], "alpha-admin")
        self.assertEqual(alpha["game_password"], "alpha-game")
        self.assertEqual(alpha["rcon_password"], "alpha-rcon")
        self.assertEqual(alpha["max_players"], 48)
        self.assertTrue(alpha["is_favourite"])
        self.assertEqual(alpha["game_properties"], {"serverMaxViewDistance": 2500})
        self.assertEqual(
            [(m["mod_guid"], m["load_order"], m["enabled"]) for m in alpha["mods"]],
            [(GUID_A, 0, True), (GUID_B, 1, False)],
        )
        self.assertEqual(alpha["mods"][0]["pinned_version"], "1.2.3")
        self.assertEqual(alpha["mods"][0]["pinned_at_build"], "0.9.8.123")
        self.assertEqual(alpha["mods"][0]["pinned_reason"], "stability")
        self.assertIsNotNone(alpha["mods"][0]["pinned_at"])
        self.assertIsNone(alpha["mods"][1]["pinned_version"])

        packs = (await self.client.get("/api/modpacks")).json()
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0]["description"], "core pack")
        self.assertEqual(
            [(i["mod_guid"], i["load_order"]) for i in packs[0]["items"]],
            [(GUID_A, 0), (GUID_C, 1)],
        )

    async def test_dry_run_changes_nothing_and_reports_actions(self) -> None:
        await self._seed_baseline()
        export = (await self.client.get("/api/backup/export")).json()

        before_servers = (await self.client.get("/api/servers")).json()
        before_packs = (await self.client.get("/api/modpacks")).json()

        # Add a brand-new definition + pack to the document.
        export["servers"].append({"name": "Charlie", "game_password": "c", "mods": []})
        export["modpacks"].append({"name": "Extra", "description": None, "items": []})

        plan = (await self.client.post("/api/backup/import?dry_run=true", json=export)).json()
        self.assertTrue(plan["dry_run"])
        self.assertEqual(
            {(p["name"], p["action"]) for p in plan["servers"]},
            {("Alpha", "skip"), ("Bravo", "skip"), ("Charlie", "create")},
        )
        self.assertEqual(
            {(p["name"], p["action"]) for p in plan["modpacks"]},
            {("Core", "skip"), ("Extra", "create")},
        )

        self.assertEqual((await self.client.get("/api/servers")).json(), before_servers)
        self.assertEqual((await self.client.get("/api/modpacks")).json(), before_packs)

    async def test_on_conflict_skip_leaves_row_untouched(self) -> None:
        await self._seed_baseline()
        export = (await self.client.get("/api/backup/export")).json()
        alpha_dump = next(s for s in export["servers"] if s["name"] == "Alpha")
        alpha_dump["game_password"] = "changed"
        alpha_dump["mods"] = [{"mod_guid": GUID_C, "mod_name": "Mod C", "load_order": 0}]

        plan = (
            await self.client.post(
                "/api/backup/import?dry_run=false&on_conflict=skip", json=export
            )
        ).json()
        self.assertEqual(
            next(p for p in plan["servers"] if p["name"] == "Alpha")["action"], "skip"
        )

        alpha = await self._server_by_name("Alpha")
        self.assertEqual(alpha["game_password"], "alpha-game")
        self.assertEqual([m["mod_guid"] for m in alpha["mods"]], [GUID_A, GUID_B])
        self.assertEqual(alpha["mods"][0]["pinned_version"], "1.2.3")

    async def test_on_conflict_replace_overwrites_and_keeps_surviving_pin(self) -> None:
        await self._seed_baseline()
        export = (await self.client.get("/api/backup/export")).json()
        alpha_dump = next(s for s in export["servers"] if s["name"] == "Alpha")
        alpha_dump["game_password"] = "replaced-game"
        alpha_dump["max_players"] = 12
        # GUID_A stays (still pinned in the doc), GUID_B leaves, GUID_C joins.
        alpha_dump["mods"] = [
            {
                "mod_guid": GUID_A,
                "mod_name": "Mod A",
                "load_order": 0,
                "enabled": True,
                "pinned_version": "1.2.3",
                "pinned_at_build": "0.9.8.123",
                "pinned_reason": "stability",
                "pinned_at": PINNED_AT.isoformat(),
            },
            {"mod_guid": GUID_C, "mod_name": "Mod C", "load_order": 1, "enabled": True},
        ]

        plan = (
            await self.client.post(
                "/api/backup/import?dry_run=false&on_conflict=replace", json=export
            )
        ).json()
        self.assertEqual(
            next(p for p in plan["servers"] if p["name"] == "Alpha")["action"], "replace"
        )

        alpha = await self._server_by_name("Alpha")
        self.assertEqual(alpha["game_password"], "replaced-game")
        self.assertEqual(alpha["max_players"], 12)
        self.assertEqual([m["mod_guid"] for m in alpha["mods"]], [GUID_A, GUID_C])
        surviving = next(m for m in alpha["mods"] if m["mod_guid"] == GUID_A)
        self.assertEqual(surviving["pinned_version"], "1.2.3")
        self.assertEqual(surviving["pinned_reason"], "stability")
        self.assertIsNotNone(surviving["pinned_at"])

    async def test_replace_skips_the_running_server(self) -> None:
        await self._seed_baseline()
        export = (await self.client.get("/api/backup/export")).json()
        alpha = await self._server_by_name("Alpha")
        self._mark_running(alpha["id"])

        alpha_dump = next(s for s in export["servers"] if s["name"] == "Alpha")
        alpha_dump["game_password"] = "should-not-apply"
        alpha_dump["mods"] = []

        plan = (
            await self.client.post(
                "/api/backup/import?dry_run=false&on_conflict=replace", json=export
            )
        ).json()
        alpha_plan = next(p for p in plan["servers"] if p["name"] == "Alpha")
        self.assertEqual(alpha_plan["action"], "skip")
        self.assertIsNotNone(alpha_plan["note"])
        # Bravo is not running -> it is still replaced.
        self.assertEqual(
            next(p for p in plan["servers"] if p["name"] == "Bravo")["action"], "replace"
        )

        alpha = await self._server_by_name("Alpha")
        self.assertEqual(alpha["game_password"], "alpha-game")
        self.assertEqual([m["mod_guid"] for m in alpha["mods"]], [GUID_A, GUID_B])

    async def test_running_server_is_also_skipped_in_the_dry_run_plan(self) -> None:
        await self._seed_baseline()
        export = (await self.client.get("/api/backup/export")).json()
        alpha = await self._server_by_name("Alpha")
        self._mark_running(alpha["id"])

        plan = (
            await self.client.post(
                "/api/backup/import?dry_run=true&on_conflict=replace", json=export
            )
        ).json()
        self.assertEqual(
            next(p for p in plan["servers"] if p["name"] == "Alpha")["action"], "skip"
        )

    async def test_unknown_mod_guid_is_still_imported(self) -> None:
        doc = {
            "version": 1,
            "servers": [
                {
                    "name": "Fresh",
                    "mods": [
                        {"mod_guid": "DEADBEEFDEADBEEF", "mod_name": "Ghost Mod", "load_order": 0}
                    ],
                }
            ],
            "modpacks": [],
        }
        plan = (await self.client.post("/api/backup/import?dry_run=false", json=doc)).json()
        self.assertEqual(plan["servers"], [{"name": "Fresh", "action": "create", "note": None}])
        fresh = await self._server_by_name("Fresh")
        self.assertEqual(fresh["mods"][0]["mod_guid"], "DEADBEEFDEADBEEF")
        self.assertEqual(fresh["mods"][0]["mod_name"], "Ghost Mod")

    async def test_malformed_document_is_422(self) -> None:
        self.assertEqual(
            (await self.client.post("/api/backup/import", json={"version": 1})).status_code, 422
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/api/backup/import", json={"servers": "nope", "modpacks": []}
                )
            ).status_code,
            422,
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/api/backup/import", json={"servers": [{"mods": []}], "modpacks": []}
                )
            ).status_code,
            422,
        )


if __name__ == "__main__":
    unittest.main()
