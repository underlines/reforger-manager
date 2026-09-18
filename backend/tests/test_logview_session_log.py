"""Session-log selection tests for :mod:`app.mods.logview`.

Self-contained on purpose: this module deals with real filesystem paths, not
DB rows, so the tests use ``tmp_path`` and real files instead of the
async-sqlite fixture pattern used elsewhere in the suite.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings as app_config
from app.core.db import Base, get_session
from app.mods import logview  # noqa: E402
from app.models import Server


class _FakeSettings:
    """Stands in for the real settings object with a tmp profiles root."""

    def __init__(self, profiles_root: Path) -> None:
        self.profiles_dir = profiles_root

    def profile_dir(self, server_id: int) -> Path:
        return self.profiles_dir / str(server_id)


def _stamp(year: int, month: int, day: int) -> float:
    return datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def test_newest_session_dir_wins(tmp_path: Path, monkeypatch) -> None:
    """The newest ``logs_*`` session dir is read; the aggregate is ignored."""
    server_id = 7
    logs = tmp_path / str(server_id) / "logs"
    older = logs / "logs_20260917T120000Z"
    newer = logs / "logs_20260918T120000Z"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "console.log").write_text("older session line\n", encoding="utf-8")
    (newer / "console.log").write_text("newer session line\n", encoding="utf-8")
    (logs / "console.log").write_text("aggregate capture line\n", encoding="utf-8")

    older_stamp = _stamp(2026, 9, 17)
    newer_stamp = _stamp(2026, 9, 18)
    os.utime(older, (older_stamp, older_stamp))
    os.utime(newer, (newer_stamp, newer_stamp))

    monkeypatch.setattr(logview, "settings", _FakeSettings(tmp_path))

    log = logview.current_log(server_id)
    assert log.exists is True
    assert log.source == "session"
    assert log.path == (newer / "console.log").resolve()
    assert [line.text for line in logview.tail_log_lines(server_id, 10)] == [
        "newer session line"
    ]


def test_falls_back_to_aggregate_when_no_session_dirs(tmp_path: Path, monkeypatch) -> None:
    """Without any ``logs_*`` directory the aggregate capture is read."""
    server_id = 3
    logs = tmp_path / str(server_id) / "logs"
    logs.mkdir(parents=True)
    # A stray ``logs_``-prefixed *file* must not count as a session dir.
    (logs / "logs_20260101T000000Z").write_text("not a dir\n", encoding="utf-8")
    aggregate = logs / "console.log"
    aggregate.write_text("aggregate capture line\n", encoding="utf-8")

    monkeypatch.setattr(logview, "settings", _FakeSettings(tmp_path))

    log = logview.current_log(server_id)
    assert log.exists is True
    assert log.source == "aggregate"
    assert log.path == aggregate.resolve()
    assert [line.text for line in logview.tail_log_lines(server_id, 10)] == [
        "aggregate capture line"
    ]


def test_traversal_escape_is_refused(tmp_path: Path, monkeypatch) -> None:
    """A session path resolving outside the profile dir is never read."""
    server_id = 9
    logs = tmp_path / str(server_id) / "logs"
    logs.mkdir(parents=True)
    # The target really exists, so a refusal proves the path check fired.
    outside = tmp_path / "escape"
    outside.mkdir()
    (outside / "console.log").write_text("outside the profile dir\n", encoding="utf-8")

    monkeypatch.setattr(logview, "settings", _FakeSettings(tmp_path))
    escape_dir = logs / ".." / ".." / "escape"
    monkeypatch.setattr(logview, "_newest_session_dir", lambda _logs_dir: escape_dir)

    log = logview.current_log(server_id)
    assert log.exists is False
    assert logview.read_log_download(server_id) is None
    assert list(logview.iter_log_lines(server_id)) == []


class GetLogRouteSourceTests(unittest.IsolatedAsyncioTestCase):
    """Route-level regression: GET /servers/{id}/log must surface `source`.

    Caught live (2026-09-18): `current_log()` computes `LogFile.source`, but
    the `/log` route only ever read `.exists` off it and never put `source`
    in the JSON response — every caller (the frontend console tab, the MCP
    tool) was blind to which file was actually read, silently defeating the
    whole point of S6.
    """

    async def asyncSetUp(self) -> None:
        from app.core.security import get_current_user
        from app.main import app

        self.app = app
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: "test-user"
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        async with sessions() as session:
            session.add(Server(id=11, name="logsrc"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.app.dependency_overrides.pop(get_session, None)
        from app.core.security import get_current_user

        self.app.dependency_overrides.pop(get_current_user, None)
        await self.engine.dispose()

    async def test_aggregate_fallback_reports_source(self) -> None:
        profiles = Path(tempfile.mkdtemp())
        logs = profiles / "11" / "logs"
        logs.mkdir(parents=True)
        (logs / "console.log").write_text("line one\n", encoding="utf-8")

        with patch.object(app_config, "profiles_dir", profiles):
            resp = await self.client.get("/api/servers/11/log")
            body = resp.json()
            self.assertEqual(body["source"], "aggregate")

    async def test_session_log_reports_source(self) -> None:
        profiles = Path(tempfile.mkdtemp())
        logs = profiles / "11" / "logs"
        session_dir = logs / "logs_20260918T120000Z"
        session_dir.mkdir(parents=True)
        (session_dir / "console.log").write_text("live session line\n", encoding="utf-8")
        (logs / "console.log").write_text("stale aggregate line\n", encoding="utf-8")

        with patch.object(app_config, "profiles_dir", profiles):
            resp = await self.client.get("/api/servers/11/log")
            body = resp.json()
            self.assertEqual(body["source"], "session")
            self.assertEqual([l["text"] for l in body["lines"]], ["live session line"])
