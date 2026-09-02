"""Route-level integration checks for the modpack CRUD API (S13)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import Mod
from app.schemas.modpack import ModpackCreate, ModpackItemIn

GUID_A = "AAAABBBBCCCCDDDD"
GUID_B = "1111222233334444"
GUID_C = "9999888877776666"


class ModpackRouteTests(unittest.IsolatedAsyncioTestCase):
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

    async def _create(self, name: str, items: list[dict], description: str | None = None) -> dict:
        response = await self.client.post(
            "/api/modpacks", json={"name": name, "description": description, "items": items}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # ----------------------------------------------------------------- tests
    async def test_create_and_read_back_resolves_mod_name(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        created = await self._create("Infantry", [{"mod_guid": GUID_A, "load_order": 3}], "desc")

        self.assertEqual(created["name"], "Infantry")
        self.assertEqual(created["description"], "desc")
        self.assertEqual(created["items"], [{"mod_guid": GUID_A, "load_order": 3, "mod_name": "Mod A"}])
        self.assertIsNotNone(created["created_at"])

        read = await self.client.get(f"/api/modpacks/{created['id']}")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json(), created)

    async def test_read_back_without_library_row_has_null_mod_name(self) -> None:
        created = await self._create("Bare", [{"mod_guid": GUID_A, "load_order": 0}])
        self.assertIsNone(created["items"][0]["mod_name"])

    async def test_list_returns_all_packs(self) -> None:
        await self._create("One", [])
        await self._create("Two", [{"mod_guid": GUID_B, "load_order": 0}])

        response = await self.client.get("/api/modpacks")
        self.assertEqual(response.status_code, 200)
        packs = response.json()
        self.assertEqual([p["name"] for p in packs], ["One", "Two"])
        self.assertEqual(packs[1]["items"][0]["mod_guid"], GUID_B)

    async def test_patch_items_replaces_the_set(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        await self._seed_mod(GUID_C, "Mod C")
        pack = await self._create(
            "Loadout",
            [{"mod_guid": GUID_A, "load_order": 0}, {"mod_guid": GUID_B, "load_order": 1}],
        )

        # A survives the replace, B leaves, C enters: must not trip uq_modpack_item.
        response = await self.client.patch(
            f"/api/modpacks/{pack['id']}",
            json={"items": [{"mod_guid": GUID_C, "load_order": 5}, {"mod_guid": GUID_A, "load_order": 1}]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(
            [(i["mod_guid"], i["load_order"]) for i in items],
            [(GUID_A, 1), (GUID_C, 5)],
        )
        self.assertEqual([i["mod_name"] for i in items], ["Mod A", "Mod C"])

        # Name/description-only PATCH leaves items untouched.
        response = await self.client.patch(
            f"/api/modpacks/{pack['id']}", json={"name": "Renamed", "description": None}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["name"], "Renamed")
        self.assertIsNone(body["description"])
        self.assertEqual(len(body["items"]), 2)

    async def test_delete_removes_pack_and_items(self) -> None:
        pack = await self._create("Doomed", [{"mod_guid": GUID_A, "load_order": 0}])

        response = await self.client.delete(f"/api/modpacks/{pack['id']}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual((await self.client.get(f"/api/modpacks/{pack['id']}")).status_code, 404)
        self.assertEqual((await self.client.get("/api/modpacks")).json(), [])

    async def test_missing_pack_is_404(self) -> None:
        self.assertEqual((await self.client.get("/api/modpacks/9999")).status_code, 404)
        self.assertEqual(
            (await self.client.patch("/api/modpacks/9999", json={"name": "x"})).status_code, 404
        )
        self.assertEqual((await self.client.delete("/api/modpacks/9999")).status_code, 404)

    async def test_duplicate_guid_in_one_payload_is_422(self) -> None:
        items = [{"mod_guid": GUID_A, "load_order": 0}, {"mod_guid": GUID_A, "load_order": 1}]
        response = await self.client.post("/api/modpacks", json={"name": "Dup", "items": items})
        self.assertEqual(response.status_code, 422)
        self.assertIn("duplicate mod_guid", response.text)

        pack = await self._create("Target", [{"mod_guid": GUID_B, "load_order": 0}])
        response = await self.client.patch(f"/api/modpacks/{pack['id']}", json={"items": items})
        self.assertEqual(response.status_code, 422)

    async def test_duplicate_pack_name_is_409(self) -> None:
        await self._create("Collision", [])
        response = await self.client.post(
            "/api/modpacks", json={"name": "Collision", "items": []}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("already exists", response.text)

        other = await self._create("Other", [])
        response = await self.client.patch(
            f"/api/modpacks/{other['id']}", json={"name": "Collision"}
        )
        self.assertEqual(response.status_code, 409, response.text)

    async def test_duplicate_modpack_item_pair_surfaces_as_409(self) -> None:
        """The uq_modpack_item catch: bypass the 422 validator via model_construct so
        the DB constraint itself fires from the flush."""
        from app.api import modpacks as modpacks_api

        async with self.sessions() as session:
            body = ModpackCreate.model_construct(
                name="Constraint",
                description=None,
                items=[
                    ModpackItemIn(mod_guid=GUID_A, load_order=0),
                    ModpackItemIn(mod_guid=GUID_A, load_order=1),
                ],
            )
            with self.assertRaises(HTTPException) as caught:
                await modpacks_api.create_modpack(body, session)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertIn("duplicate", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
