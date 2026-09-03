"""S13 route tests — the per-server profile Files tab.

App-style route tests (the ``test_storage_and_download`` pattern): a throwaway
app with ``get_session`` / ``get_current_user`` overridden and
``settings.profiles_dir`` pointed at a temp dir so the files are real but local.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import server_files as server_files_api
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import Server


class ServerFilesRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(server_files_api.router)
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-server-files-test-"))
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.profiles_patch = patch.object(settings, "profiles_dir", self.profiles)
        self.profiles_patch.start()

        async with self.sessions() as session:
            session.add(Server(id=1, name="srv1"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        self.profiles_patch.stop()
        await self.client.aclose()
        await self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _profile(self, server_id: int = 1) -> Path:
        return settings.profiles_dir / str(server_id)

    def _write(self, rel_path: str, data: bytes, server_id: int = 1) -> Path:
        target = self._profile(server_id) / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    # ------------------------------------------------------------------ tests
    async def test_seeded_tree_lists_and_drops_system_dirs(self) -> None:
        self._write("sub/nested.txt", b"hi")
        self._write("root.txt", b"data")
        self._write("logs/console.log", b"synthetic seed file used by test only")
        self._write("addons_tmp/stray.bin", b"synthetic seed file used by test only")

        response = await self.client.get("/servers/1/files")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["server_id"], 1)
        names = {e["name"] for e in data["entries"]}
        self.assertEqual(names, {"sub", "root.txt"})
        self.assertNotIn("logs", names)
        self.assertNotIn("addons_tmp", names)

    async def test_missing_profile_dir_lists_empty(self) -> None:
        response = await self.client.get("/servers/1/files")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["entries"], [])

    async def test_big_file_not_editable_but_downloadable(self) -> None:
        payload = b"x" * 40
        self._write("big.bin", payload)
        with patch.object(settings, "files_max_edit_bytes", 8):
            response = await self.client.get("/servers/1/files/content", params={"path": "big.bin"})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["size"], 40)
        self.assertFalse(data["editable"])
        self.assertIsNone(data["content"])

        response = await self.client.get("/servers/1/files/download", params={"path": "big.bin"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, payload)

    async def test_binary_file_not_editable(self) -> None:
        self._write("bin.dat", b"ab\x00cd")
        response = await self.client.get("/servers/1/files/content", params={"path": "bin.dat"})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertFalse(data["editable"])
        self.assertIsNone(data["content"])

    async def test_archive_skips_symlinks_and_round_trips_files(self) -> None:
        self._write("keep.txt", b"roundtrip")
        link = self._profile() / "link"
        try:
            link.symlink_to(self._profile() / "keep.txt")
        except OSError as exc:
            self.skipTest(f"symlinks unsupported here: {exc}")

        response = await self.client.get("/servers/1/files/archive")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = zf.namelist()
        self.assertNotIn("link", names)
        self.assertIn("keep.txt", names)
        self.assertEqual(zf.read("keep.txt"), b"roundtrip")

    async def test_unknown_server_is_404(self) -> None:
        response = await self.client.get("/servers/999/files")
        self.assertEqual(response.status_code, 404, response.text)

    async def test_path_traversal_is_400(self) -> None:
        self._write("root.txt", b"data")
        response = await self.client.get("/servers/1/files", params={"path": "../.."})
        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()

class ServerFilesMutationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(server_files_api.router)
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-server-files-mut-test-"))
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.profiles_patch = patch.object(settings, "profiles_dir", self.profiles)
        self.profiles_patch.start()

        async with self.sessions() as session:
            session.add(Server(id=1, name="srv1"))
            await session.commit()

        self.supervisor_patches: list[patch] = []
        self.is_running_patch = patch.object(
            server_files_api.supervisor, "is_running", lambda: False
        )
        self.is_running_patch.start()
        self.supervisor_patches.append(self.is_running_patch)

    async def asyncTearDown(self) -> None:
        for p in self.supervisor_patches:
            p.stop()
        self.profiles_patch.stop()
        await self.client.aclose()
        await self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _profile(self, server_id: int = 1) -> Path:
        return settings.profiles_dir / str(server_id)

    def _write(self, rel_path: str, data: bytes, server_id: int = 1) -> Path:
        target = self._profile(server_id) / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def _patch_running(self, server_id: int = 1) -> None:
        from unittest.mock import PropertyMock

        active_patch = patch.object(
            type(server_files_api.supervisor),
            "active_server_id",
            PropertyMock(return_value=server_id),
        )
        active_patch.start()
        self.supervisor_patches.append(active_patch)
        self.is_running_patch.stop()
        running_patch = patch.object(
            server_files_api.supervisor, "is_running", lambda: True
        )
        running_patch.start()
        self.supervisor_patches.append(running_patch)

    # ------------------------------------------------------------------ tests
    async def test_409_matrix_while_running_and_reads_still_ok(self) -> None:
        self._write("root.txt", b"data")
        self._write("sub/nested.txt", b"hi")
        self._write("cfg.json", b'{"a": 1}')
        self._patch_running()

        cases = [
            await self.client.put(
                "/servers/1/files/content",
                params={"path": "root.txt"},
                json={"content": "new"},
            ),
            await self.client.delete("/servers/1/files", params={"path": "root.txt"}),
            await self.client.post("/servers/1/files/mkdir", json={"path": "newdir"}),
            await self.client.post(
                "/servers/1/files/rename",
                json={"path": "root.txt", "new_path": "renamed.txt"},
            ),
            await self.client.post(
                "/servers/1/files/upload",
                params={"path": "sub"},
                files={"files": ("up.txt", b"hello")},
            ),
        ]
        for response in cases:
            self.assertEqual(response.status_code, 409, response.text)

        for response in (
            await self.client.get("/servers/1/files"),
            await self.client.get("/servers/1/files/content", params={"path": "root.txt"}),
            await self.client.get("/servers/1/files/download", params={"path": "root.txt"}),
            await self.client.get("/servers/1/files/archive"),
        ):
            self.assertEqual(response.status_code, 200, response.text)

    async def test_put_content_create_overwrite_and_invalid_json(self) -> None:
        response = await self.client.put(
            "/servers/1/files/content",
            params={"path": "new.txt"},
            json={"content": "v1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self._profile() / "new.txt").read_text(), "v1")

        response = await self.client.put(
            "/servers/1/files/content",
            params={"path": "new.txt"},
            json={"content": "v2-longer"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self._profile() / "new.txt").read_text(), "v2-longer")

        await self.client.put(
            "/servers/1/files/content",
            params={"path": "cfg.json"},
            json={"content": '{"ok": true}'},
        )
        response = await self.client.put(
            "/servers/1/files/content",
            params={"path": "cfg.json"},
            json={"content": "{not json"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            (self._profile() / "cfg.json").read_text(), '{"ok": true}'
        )

    async def test_mkdir_then_listed(self) -> None:
        response = await self.client.post("/servers/1/files/mkdir", json={"path": "brand/dir"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self._profile() / "brand" / "dir").is_dir())

        response = await self.client.get("/servers/1/files")
        names = {e["name"] for e in response.json()["entries"]}
        self.assertIn("brand", names)

    async def test_upload_happy_too_big_and_traversal_filename(self) -> None:
        response = await self.client.post(
            "/servers/1/files/upload",
            files={"files": ("hello.txt", b"world")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self._profile() / "hello.txt").read_bytes(), b"world")

        with patch.object(settings, "files_max_upload_bytes", 4):
            response = await self.client.post(
                "/servers/1/files/upload",
                files={"files": ("big.bin", b"x" * 100)},
            )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertFalse((self._profile() / "big.bin").exists())
        self.assertFalse(any((self._profile()).glob(".*upload*")))

        response = await self.client.post(
            "/servers/1/files/upload",
            files={"files": ("../../evil.txt", b"boom")},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertFalse((self.tmp.parent / "evil.txt").exists())
        self.assertFalse((self._profile() / "evil.txt").exists())

    async def test_delete_nonempty_directory_recursively(self) -> None:
        self._write("sub/nested.txt", b"hi")
        self._write("sub/deeper/x.bin", b"b")
        response = await self.client.delete("/servers/1/files", params={"path": "sub"})
        self.assertEqual(response.status_code, 204, response.text)
        self.assertFalse((self._profile() / "sub").exists())

    async def test_rename_onto_existing_is_400(self) -> None:
        self._write("a.txt", b"a")
        self._write("b.txt", b"b")
        response = await self.client.post(
            "/servers/1/files/rename",
            json={"path": "a.txt", "new_path": "b.txt"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue((self._profile() / "a.txt").exists())
        self.assertEqual((self._profile() / "b.txt").read_bytes(), b"b")
