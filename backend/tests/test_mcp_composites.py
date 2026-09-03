"""S6: the composite read-only tools, driven at the protocol level.

Same harness as the S4/S5 tool tests (a *fresh* ``mcp_app()`` per round-trip —
the streamable session manager is single-use and ASGITransport never sends
lifespan — so the inner app's lifespan is entered manually, one initialize per
round-trip). The contract under test:

- ``wait_for_job`` blocks on a job enqueued through the real ``JobManager``
  (a fake handler registered for a probe kind; the manager's session factory
  is pointed at the test DB) and returns its terminal ``JobOut`` — state,
  result, log tail; a still-running job past the deadline comes back with
  ``timed_out: true`` and the current state, not an error; an unknown id is
  the deterministic 404 tool error;
- ``tail_log`` slices the log endpoint's stream to the last ``lines`` entries
  (clamped 1..1000) on a written fixture console.log, honouring ``hide_spam``;
- ``server_overview`` returns the one-dict situation report: servers, the
  running id (derived API-first from the definitions' ``is_running`` flag the
  supervisor maintains), the engine row and the 5 newest jobs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import Base, get_session
from app.core.jobs import job_manager
from app.models import ENGINE_SINGLETON_ID, Job, JobState, McpToken, Server, User

# A job kind the test env owns: registered here with a fake handler so the
# real JobManager worker drives it to a terminal state (the enqueue-mock
# precedent from test_mcp_tools_mutations.py cannot show a *transition*).
PROBE_JOB_KIND = "test_wait_probe"

SPAM_TEXT = "thermalprofiledefault.conf"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _first_text(result) -> str:
    return next(c.text for c in result.content if getattr(c, "type", None) == "text")


def _result_json(result) -> object:
    return json.loads(_first_text(result))


class _Fixture:
    """Real app, sqlite sessions, patched middleware lookup (S3 pattern)."""

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

        self._overridden = (get_session,)
        app.dependency_overrides[get_session] = override_session
        # The middleware opens its own session; point it at the test DB.
        self._session_patch = patch("app.mcp.auth.SessionLocal", self.sessions)
        self._session_patch.start()

        async with self.sessions() as session:
            session.add(
                User(
                    username=settings.admin_username,
                    password_hash="not-a-real-hash",
                    is_admin=True,
                    is_active=True,
                )
            )
            await session.commit()

        # An MCP bearer token the middleware accepts (minted here; the REST
        # route needs no extra setup in this sqlite world, but seeding keeps
        # every fixture independent of the S2 token REST).
        self.raw_token = "rfm_" + "d4" * 16
        async with self.sessions() as session:
            session.add(McpToken(label="sdk", token_hash=_sha256(self.raw_token)))
            await session.commit()

    async def asyncTearDown(self) -> None:
        self._session_patch.stop()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        await self.engine.dispose()

    # ---------------------------------------------------------------- helpers
    async def _seed_server(self, name: str = "main", **extra) -> Server:
        async with self.sessions() as session:
            row = Server(name=name, **extra)
            session.add(row)
            await session.commit()
            return row

    @asynccontextmanager
    async def mcp_session(self):
        """A fresh mcp_app() per use: lifespan entered manually, then initialize."""
        from app.mcp import mcp_app

        wrapper = mcp_app()
        inner = wrapper.app  # McpAuthMiddleware -> Starlette streamable app
        transport = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=wrapper),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {self.raw_token}"},
        )
        try:
            # ASGITransport never runs lifespans: start the session manager the
            # way main.py's lifespan does in production.
            async with inner.router.lifespan_context(inner):
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=transport
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
        finally:
            await transport.aclose()


class WaitForJobTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """Polling to a terminal state, the timeout marker, and the 404 mapping."""

    async def test_wait_for_job_returns_the_final_job_out(self) -> None:
        async def handler(ctx) -> None:
            await ctx.log("fake handler ran")
            return {"ok": True}

        job_manager.register(PROBE_JOB_KIND, handler)
        try:
            with patch.object(job_manager, "_session_factory", self.sessions):
                await job_manager.start()
                try:
                    job_id = await job_manager.enqueue(PROBE_JOB_KIND)
                    async with self.mcp_session() as session:
                        result = await session.call_tool(
                            "wait_for_job",
                            {"job_id": job_id, "timeout_s": 10, "poll_s": 0.01},
                        )
                finally:
                    await job_manager.stop()
        finally:
            job_manager._factories.pop(PROBE_JOB_KIND, None)

        self.assertFalse(result.is_error, _first_text(result))
        body = _result_json(result)
        self.assertEqual(body["id"], job_id)
        self.assertEqual(body["state"], "succeeded")
        self.assertEqual(body["result"], {"ok": True})
        self.assertIn("fake handler ran", body["log_tail"])
        self.assertIsNotNone(body["finished_at"])
        self.assertNotIn("timed_out", body)

    async def test_wait_for_job_timeout_returns_timed_out_marker(self) -> None:
        async with self.sessions() as session:
            job = Job(kind="mod_sync")
            job.state = JobState.running
            job.current_step = "halfway"
            session.add(job)
            await session.commit()
            job_id = job.id

        async with self.mcp_session() as session:
            result = await session.call_tool(
                "wait_for_job", {"job_id": job_id, "timeout_s": 1, "poll_s": 0.05}
            )

        # Never raises: a still-running job past the deadline is a marker, not
        # an error — the caller can simply poll again.
        self.assertFalse(result.is_error, _first_text(result))
        body = _result_json(result)
        self.assertIs(body["timed_out"], True)
        self.assertEqual(body["state"], "running")
        self.assertEqual(body["current_step"], "halfway")
        self.assertIsNone(body["finished_at"])

    async def test_wait_for_job_unknown_id_is_a_deterministic_error(self) -> None:
        async with self.mcp_session() as session:
            result = await session.call_tool(
                "wait_for_job", {"job_id": 424242, "timeout_s": 1, "poll_s": 0.05}
            )
        self.assertTrue(result.is_error)
        self.assertIn("404", _first_text(result))


class TailLogTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """Slicing + spam filtering on a written fixture console.log."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="rfmr-mcp-composites-"))
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir()
        self._profiles_patch = patch.object(settings, "profiles_dir", self.profiles)
        self._profiles_patch.start()

    async def asyncTearDown(self) -> None:
        self._profiles_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        await super().asyncTearDown()

    async def _seed_server_with_log(self, total_lines: int = 30) -> Server:
        row = await self._seed_server("main")
        logs = self.profiles / str(row.id) / "logs"
        logs.mkdir(parents=True)
        lines = [f"line {i:03d}" for i in range(1, total_lines + 1)]
        lines[4] = f"NOTE: {SPAM_TEXT} was applied"  # cold-cache spam default
        if total_lines > 5:
            lines[5] = "SCRIPT (W): something odd"  # severity-tagged
        (logs / "console.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return row

    async def test_tail_log_returns_the_last_lines(self) -> None:
        row = await self._seed_server_with_log(total_lines=30)
        async with self.mcp_session() as session:
            result = await session.call_tool(
                "tail_log", {"server_id": row.id, "lines": 10}
            )
        self.assertFalse(result.is_error, _first_text(result))
        body = _result_json(result)
        self.assertEqual(body["server_id"], row.id)
        texts = [line["text"] for line in body["lines"]]
        # 30 written lines minus the one spam hit = 29 streamed; last 10.
        self.assertEqual(texts, [f"line {i:03d}" for i in range(21, 31)])
        self.assertEqual(body["lines"][-1]["line_number"], 30)

    async def test_tail_log_clamps_lines_to_1_1000(self) -> None:
        row = await self._seed_server_with_log(total_lines=5)
        async with self.mcp_session() as session:
            huge = await session.call_tool(
                "tail_log", {"server_id": row.id, "lines": 5000}
            )
            one = await session.call_tool(
                "tail_log", {"server_id": row.id, "lines": 0}
            )
        self.assertFalse(huge.is_error, _first_text(huge))
        self.assertEqual(len(_result_json(huge)["lines"]), 4)  # 5 minus the spam line
        self.assertFalse(one.is_error, _first_text(one))
        # Clamp to 1 line; line 005 is the spam hit, so 004 is the last visible.
        self.assertEqual(_result_json(one)["lines"], _result_json(huge)["lines"][-1:])

    async def test_tail_log_honours_hide_spam(self) -> None:
        row = await self._seed_server_with_log(total_lines=10)
        async with self.mcp_session() as session:
            hidden = await session.call_tool(
                "tail_log", {"server_id": row.id, "lines": 100}
            )
            shown = await session.call_tool(
                "tail_log", {"server_id": row.id, "lines": 100, "hide_spam": False}
            )
        self.assertFalse(hidden.is_error, _first_text(hidden))
        self.assertFalse(shown.is_error, _first_text(shown))
        hidden_texts = [line["text"] for line in _result_json(hidden)["lines"]]
        shown = _result_json(shown)["lines"]
        self.assertNotIn(SPAM_TEXT, " ".join(hidden_texts))
        spam_row = next(line for line in shown if SPAM_TEXT in line["text"])
        self.assertTrue(spam_row["is_spam"])
        self.assertEqual(spam_row["line_number"], 5)
        warning = next(line for line in shown if line["text"] == "SCRIPT (W): something odd")
        self.assertEqual(warning["severity"], "warning")


class ServerOverviewTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """The one-dict situation report: servers + running id + engine + jobs."""

    async def test_overview_shape(self) -> None:
        stopped = await self._seed_server("stopped")
        running = await self._seed_server("running", is_running=True)
        async with self.sessions() as session:
            for i in range(7):
                session.add(Job(kind=f"probe{i}"))
            await session.commit()

        async with self.mcp_session() as session:
            result = await session.call_tool("server_overview", {})

        self.assertFalse(result.is_error, _first_text(result))
        body = _result_json(result)
        self.assertEqual(
            set(body), {"servers", "running_server_id", "engine", "recent_jobs"}
        )
        self.assertEqual([s["id"] for s in body["servers"]], [stopped.id, running.id])
        self.assertEqual(body["running_server_id"], running.id)
        # The 5 most recent jobs, newest first — 7 seeded, only 5 returned.
        self.assertEqual(
            [j["kind"] for j in body["recent_jobs"]],
            [f"probe{i}" for i in (6, 5, 4, 3, 2)],
        )
        self.assertEqual(body["engine"]["id"], ENGINE_SINGLETON_ID)

    async def test_overview_without_running_server_reports_none(self) -> None:
        row = await self._seed_server("idle")
        async with self.mcp_session() as session:
            result = await session.call_tool("server_overview", {})
        self.assertFalse(result.is_error, _first_text(result))
        body = _result_json(result)
        self.assertIsNone(body["running_server_id"])
        self.assertEqual([s["id"] for s in body["servers"]], [row.id])
        self.assertEqual(body["recent_jobs"], [])
        self.assertEqual(body["engine"]["id"], ENGINE_SINGLETON_ID)


if __name__ == "__main__":
    unittest.main()
