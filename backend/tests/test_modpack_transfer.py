"""Route-level checks for modpack apply / create-from-server / import-export (S15)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import Mod, Modpack, ModpackItem, Server, ServerMod

GUID_A = "AAAABBBBCCCCDDDD"
GUID_B = "1111222233334444"
GUID_C = "9999888877776666"
GUID_D = "5555666677778888"


class ModpackTransferTests(unittest.IsolatedAsyncioTestCase):
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

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        await self.engine.dispose()

    # ---------------------------------------------------------------- helpers
    async def _seed_mod(self, guid: str, name: str | None) -> None:
        async with self.sessions() as session:
            session.add(Mod(guid=guid, name=name))
            await session.commit()

    async def _seed_server(
        self, name: str, mods: list[dict], *, is_running: bool = False
    ) -> int:
        async with self.sessions() as session:
            server = Server(name=name, is_running=is_running)
            for spec in mods:
                server.mods.append(
                    ServerMod(
                        mod_guid=spec["mod_guid"],
                        mod_name=spec.get("mod_name"),
                        load_order=spec.get("load_order", 0),
                        enabled=spec.get("enabled", True),
                        pinned_version=spec.get("pinned_version"),
                        pinned_reason=spec.get("pinned_reason"),
                    )
                )
            session.add(server)
            await session.commit()
            return server.id

    async def _seed_pack(self, name: str, items: list[dict], description: str | None = None) -> int:
        async with self.sessions() as session:
            pack = Modpack(name=name, description=description)
            for spec in items:
                pack.items.append(
                    ModpackItem(mod_guid=spec["mod_guid"], load_order=spec.get("load_order", 0))
                )
            session.add(pack)
            await session.commit()
            return pack.id

    async def _server_mods(self, server_id: int) -> list[dict]:
        response = await self.client.get(f"/api/servers/{server_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["mods"]

    # ------------------------------------------------------------- apply: replace
    async def test_apply_replace_preserves_surviving_pin_and_reports_dropped(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        await self._seed_mod(GUID_B, "Mod B")
        await self._seed_mod(GUID_C, "Mod C")
        server_id = await self._seed_server(
            "srv",
            [
                {"mod_guid": GUID_A, "mod_name": "Mod A", "load_order": 0, "pinned_version": "1.0.0"},
                {"mod_guid": GUID_B, "mod_name": "Mod B", "load_order": 1, "pinned_version": "2.0.0"},
            ],
        )
        pack_id = await self._seed_pack(
            "pack", [{"mod_guid": GUID_A, "load_order": 0}, {"mod_guid": GUID_C, "load_order": 1}]
        )

        response = await self.client.post(
            f"/api/modpacks/{pack_id}/apply/{server_id}", json={"mode": "replace"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["applied"], 2)
        self.assertEqual(body["mode"], "replace")
        self.assertEqual(body["dropped_pins"], [{"mod_guid": GUID_B, "mod_name": "Mod B"}])

        mods = await self._server_mods(server_id)
        by_guid = {m["mod_guid"]: m for m in mods}
        self.assertEqual(set(by_guid), {GUID_A, GUID_C})
        self.assertEqual(by_guid[GUID_A]["pinned_version"], "1.0.0")  # survived
        self.assertIsNone(by_guid[GUID_C]["pinned_version"])

    async def test_apply_replace_pack_still_has_pin_when_nothing_dropped(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        server_id = await self._seed_server(
            "srv",
            [{"mod_guid": GUID_A, "mod_name": "Mod A", "load_order": 0, "pinned_version": "1.2.3"}],
        )
        pack_id = await self._seed_pack("pack", [{"mod_guid": GUID_A, "load_order": 0}])

        response = await self.client.post(
            f"/api/modpacks/{pack_id}/apply/{server_id}", json={"mode": "replace"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["dropped_pins"], [])
        mods = await self._server_mods(server_id)
        self.assertEqual(mods[0]["pinned_version"], "1.2.3")

    # ------------------------------------------------------------- apply: append
    async def test_apply_append_keeps_existing_and_appends_after_max(self) -> None:
        for guid, name in [(GUID_A, "Mod A"), (GUID_B, "Mod B"), (GUID_C, "Mod C"), (GUID_D, "Mod D")]:
            await self._seed_mod(guid, name)
        server_id = await self._seed_server(
            "srv",
            [
                {"mod_guid": GUID_A, "mod_name": "Mod A", "load_order": 5, "pinned_version": "9.9"},
                {"mod_guid": GUID_B, "mod_name": "Mod B", "load_order": 6},
            ],
        )
        pack_id = await self._seed_pack(
            "pack",
            [
                {"mod_guid": GUID_A, "load_order": 0},
                {"mod_guid": GUID_C, "load_order": 1},
                {"mod_guid": GUID_D, "load_order": 2},
            ],
        )

        response = await self.client.post(
            f"/api/modpacks/{pack_id}/apply/{server_id}", json={"mode": "append"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["applied"], 3)
        self.assertEqual(body["dropped_pins"], [])

        mods = await self._server_mods(server_id)
        self.assertEqual(
            [(m["mod_guid"], m["load_order"]) for m in mods],
            [(GUID_A, 5), (GUID_B, 6), (GUID_C, 7), (GUID_D, 8)],
        )
        by_guid = {m["mod_guid"]: m for m in mods}
        self.assertEqual(by_guid[GUID_A]["pinned_version"], "9.9")  # untouched, not moved

    # ------------------------------------------------------------------- guards
    async def test_apply_while_running_is_409(self) -> None:
        server_id = await self._seed_server("srv", [], is_running=True)
        pack_id = await self._seed_pack("pack", [{"mod_guid": GUID_A, "load_order": 0}])
        response = await self.client.post(
            f"/api/modpacks/{pack_id}/apply/{server_id}", json={"mode": "replace"}
        )
        self.assertEqual(response.status_code, 409, response.text)

    async def test_apply_missing_pack_or_server_is_404(self) -> None:
        server_id = await self._seed_server("srv", [])
        pack_id = await self._seed_pack("pack", [])
        self.assertEqual(
            (await self.client.post(f"/api/modpacks/9999/apply/{server_id}", json={"mode": "replace"})).status_code,
            404,
        )
        self.assertEqual(
            (await self.client.post(f"/api/modpacks/{pack_id}/apply/9999", json={"mode": "replace"})).status_code,
            404,
        )

    # --------------------------------------------------------------- from-server
    async def test_from_server_snapshots_order_and_notes_pins(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        await self._seed_mod(GUID_B, "Mod B")
        server_id = await self._seed_server(
            "srv",
            [
                {"mod_guid": GUID_B, "mod_name": "Mod B", "load_order": 0},
                {"mod_guid": GUID_A, "mod_name": "Mod A", "load_order": 1, "pinned_version": "3.1"},
            ],
        )

        response = await self.client.post(
            f"/api/modpacks/from-server/{server_id}", json={"name": "Snap"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(
            [(i["mod_guid"], i["load_order"]) for i in body["items"]],
            [(GUID_B, 0), (GUID_A, 1)],
        )
        # pack items carry no pin data
        self.assertNotIn("pinned_version", body["items"][1])
        self.assertIsNotNone(body["pins_note"])
        self.assertIn("Mod A", body["pins_note"])
        self.assertIn("3.1", body["pins_note"])

        # duplicate name -> 409
        dup = await self.client.post(
            f"/api/modpacks/from-server/{server_id}", json={"name": "Snap"}
        )
        self.assertEqual(dup.status_code, 409, dup.text)

    async def test_from_server_without_pins_has_null_note(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        server_id = await self._seed_server(
            "srv", [{"mod_guid": GUID_A, "mod_name": "Mod A", "load_order": 0}]
        )
        response = await self.client.post(
            f"/api/modpacks/from-server/{server_id}", json={"name": "NoPins"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(response.json()["pins_note"])

    async def test_from_server_missing_server_is_404(self) -> None:
        response = await self.client.post(
            "/api/modpacks/from-server/9999", json={"name": "X"}
        )
        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------ export/import
    async def test_export_import_round_trip_and_conflict_modes(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        await self._seed_mod(GUID_B, "Mod B")
        await self._seed_mod(GUID_C, "Mod C")
        pack_id = await self._seed_pack(
            "RoundTrip",
            [
                {"mod_guid": GUID_A, "load_order": 0},
                {"mod_guid": GUID_B, "load_order": 1},
                {"mod_guid": GUID_C, "load_order": 2},
            ],
            description="portable",
        )

        exported = await self.client.get(f"/api/modpacks/{pack_id}/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        doc = exported.json()
        self.assertEqual(set(doc), {"name", "description", "items"})
        self.assertEqual(
            [(i["mod_guid"], i["load_order"]) for i in doc["items"]],
            [(GUID_A, 0), (GUID_B, 1), (GUID_C, 2)],
        )

        # delete + re-import: identical items/order
        self.assertEqual((await self.client.delete(f"/api/modpacks/{pack_id}")).status_code, 204)
        reimported = await self.client.post("/api/modpacks/import", json=doc)
        self.assertEqual(reimported.status_code, 200, reimported.text)
        restored = reimported.json()
        self.assertEqual(restored["name"], "RoundTrip")
        self.assertEqual(
            [(i["mod_guid"], i["load_order"]) for i in restored["items"]],
            [(GUID_A, 0), (GUID_B, 1), (GUID_C, 2)],
        )

        # collision + rename -> distinct names
        r1 = await self.client.post("/api/modpacks/import?on_conflict=rename", json=doc)
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r1.json()["name"], "RoundTrip (imported)")
        r2 = await self.client.post("/api/modpacks/import?on_conflict=rename", json=doc)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["name"], "RoundTrip (imported 2)")

        # collision + replace -> overwrites items of the existing pack, same id
        replacement = {"name": "RoundTrip", "description": "new", "items": [{"mod_guid": GUID_C, "load_order": 0}]}
        r3 = await self.client.post("/api/modpacks/import?on_conflict=replace", json=replacement)
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual(r3.json()["id"], restored["id"])
        self.assertEqual(
            [(i["mod_guid"], i["load_order"]) for i in r3.json()["items"]], [(GUID_C, 0)]
        )
        self.assertEqual(r3.json()["description"], "new")

        # collision + error -> 409
        r4 = await self.client.post("/api/modpacks/import?on_conflict=error", json=doc)
        self.assertEqual(r4.status_code, 409, r4.text)

    async def test_import_duplicate_guid_in_items_is_422(self) -> None:
        doc = {
            "name": "Dup",
            "items": [
                {"mod_guid": GUID_A, "load_order": 0},
                {"mod_guid": GUID_A, "load_order": 1},
            ],
        }
        response = await self.client.post("/api/modpacks/import", json=doc)
        self.assertEqual(response.status_code, 422, response.text)

    async def test_export_missing_pack_is_404(self) -> None:
        self.assertEqual((await self.client.get("/api/modpacks/9999/export")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
