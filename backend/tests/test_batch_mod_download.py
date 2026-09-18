"""POST /api/mods/download (S5): one job, one engine spawn for N GUIDs.

The engine-facing ``run_mod_download`` (``app/mods/downloader.py``) is already
list-shaped and is invoked exactly once per enqueued ``mod_download`` job by
the job factory registered in ``app/main.py`` (``_job_mod_download`` -> one
call to ``run_mod_download(ctx, guids, versions)``) — that wiring is untouched
by this story. So "does the batch path call ``run_mod_download`` once, not N
times" reduces to "does the batch route call ``job_manager.enqueue`` exactly
once, with a single job whose ``guids`` param carries every requested GUID" —
that boundary (``job_manager.enqueue``) is what these tests mock, mirroring
the house pattern used for every other job-returning mods route
(``test_mcp_tools_mutations.py``'s ``test_download_mod_returns_job_enqueued_out``).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.api import mods as mods_api
from app.core.config import settings
from app.core.db import Base
from app.models import Mod
from app.mods.downloader import MOD_DOWNLOAD_JOB_KIND
from app.schemas.job import JobEnqueuedOut
from app.schemas.mod import ModBatchDownloadIn, ModDownloadIn

GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"
GUID_C = "CCCCCCCCCCCCCCCC"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as result:
        yield result
    await engine.dispose()


@pytest.fixture(autouse=True)
def _mods_dir():
    # The free-space guard (app/mods/freespace.py) calls shutil.disk_usage on
    # settings.mods_dir, which is an in-container path in the default config —
    # point it at a real temp dir for the duration of each test (house pattern,
    # see test_mcp_tools_mutations.py's _Fixture.asyncSetUp).
    with tempfile.TemporaryDirectory(prefix="rfmr-batch-download-") as tmp:
        with patch.object(settings, "mods_dir", Path(tmp)):
            yield


async def _add_mods(session, guids: list[str]) -> None:
    # size=1 (not is_local) so the free-space guard's ensure_sizes() never
    # needs to hit the Workshop client to backfill a NULL size.
    session.add_all([Mod(guid=guid, size=1) for guid in guids])
    await session.commit()


@pytest.mark.asyncio
async def test_batch_route_enqueues_exactly_one_job_with_all_guids(session):
    await _add_mods(session, [GUID_A, GUID_B, GUID_C])
    enqueue = AsyncMock(return_value=42)

    with patch.object(mods_api.job_manager, "enqueue", new=enqueue):
        result = await mods_api.download_mods(
            body=ModBatchDownloadIn(guids=[GUID_A, GUID_B, GUID_C]), session=session
        )

    assert result.job_id == 42
    assert result.kind == MOD_DOWNLOAD_JOB_KIND
    enqueue.assert_awaited_once()
    kind, kwargs = enqueue.await_args.args[0], enqueue.await_args.kwargs
    assert kind == MOD_DOWNLOAD_JOB_KIND
    assert kwargs["params"]["guids"] == [GUID_A, GUID_B, GUID_C]


@pytest.mark.asyncio
async def test_batch_route_uppercases_and_carries_version_pins(session):
    await _add_mods(session, [GUID_A, GUID_B])
    enqueue = AsyncMock(return_value=1)

    with patch.object(mods_api.job_manager, "enqueue", new=enqueue):
        await mods_api.download_mods(
            body=ModBatchDownloadIn(
                guids=[GUID_A.lower(), GUID_B.lower()], versions={GUID_A: "1.2.3"}
            ),
            session=session,
        )

    enqueue.assert_awaited_once()
    params = enqueue.await_args.kwargs["params"]
    assert params["guids"] == [GUID_A, GUID_B]
    assert params["versions"] == {GUID_A: "1.2.3"}


@pytest.mark.asyncio
async def test_single_guid_route_resolves_through_the_same_batch_path(session):
    """The old ``POST /{guid}/download`` route must still enqueue exactly one
    job with ``guids=[guid]`` — same job shape/response contract as before."""
    await _add_mods(session, [GUID_A])
    enqueue = AsyncMock(return_value=7)

    with patch.object(mods_api.job_manager, "enqueue", new=enqueue):
        result = await mods_api.download_mod(guid=GUID_A.lower(), body=None, session=session)

    assert result.job_id == 7
    assert result.kind == MOD_DOWNLOAD_JOB_KIND
    enqueue.assert_awaited_once()
    kind, kwargs = enqueue.await_args.args[0], enqueue.await_args.kwargs
    assert kind == MOD_DOWNLOAD_JOB_KIND
    assert kwargs["params"]["guids"] == [GUID_A]
    assert kwargs["params"]["versions"] == {}


@pytest.mark.asyncio
async def test_single_guid_route_carries_its_version_pin_into_the_batch_path(session):
    await _add_mods(session, [GUID_A])
    enqueue = AsyncMock(return_value=7)

    with patch.object(mods_api.job_manager, "enqueue", new=enqueue):
        await mods_api.download_mod(
            guid=GUID_A, body=ModDownloadIn(version="9.9.9"), session=session
        )

    params = enqueue.await_args.kwargs["params"]
    assert params["guids"] == [GUID_A]
    assert params["versions"] == {GUID_A: "9.9.9"}


@pytest.mark.asyncio
async def test_single_guid_wrapper_is_provably_not_a_separate_implementation(session):
    """Both routes must go through the same shared helper — not two
    independent code paths that happen to look alike."""
    await _add_mods(session, [GUID_A])
    shared = AsyncMock(return_value=JobEnqueuedOut(job_id=1, kind=MOD_DOWNLOAD_JOB_KIND))

    with patch.object(mods_api, "_enqueue_mod_download", new=shared):
        await mods_api.download_mods(body=ModBatchDownloadIn(guids=[GUID_A]), session=session)
        await mods_api.download_mod(guid=GUID_A, body=None, session=session)

    assert shared.await_count == 2
    first_call, second_call = shared.await_args_list
    assert first_call.args[1] == [GUID_A]
    assert second_call.args[1] == [GUID_A]
