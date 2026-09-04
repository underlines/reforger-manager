"""Deterministic tests for the post-download closure verification; no engine."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import main  # noqa: E402
from app.mods import downloader, scanner  # noqa: E402
from app.mods.resolve import ResolvedNode, ResolvedTree  # noqa: E402

GUID_A = "0123456789ABCDEF"
GUID_B = "FEDCBA9876543210"


class FakeContext:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.progress_updates: list[tuple[float, str | None]] = []

    async def log(self, line: str) -> None:
        self.logs.append(line)

    async def progress(self, pct: float, step: str | None = None) -> None:
        self.progress_updates.append((pct, step))


class FakeSession:
    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _make_addon(mods_root: Path, guid: str) -> None:
    addon = mods_root / "reforger" / "addons" / f"Addon_{guid}"
    addon.mkdir(parents=True, exist_ok=True)
    (addon / "meta").write_text(
        json.dumps({"meta": {"id": guid, "name": f"Addon {guid}", "versions": []}}),
        encoding="utf-8",
    )


def _fake_params(params: dict):
    async def _get(ctx) -> dict:
        return params

    return _get


async def _fake_resolve(session, root_guids, **kwargs) -> ResolvedTree:
    guids = [g.upper() for g in root_guids]
    nodes = [ResolvedNode(g, None, "unknown", "unresolved", 0) for g in guids]
    return ResolvedTree(roots=guids, nodes=nodes)


class DownloadClosureTests(unittest.IsolatedAsyncioTestCase):
    def test_partition_present_reports_missing_addon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp)
            _make_addon(mods, GUID_A)
            with patch.object(scanner, "settings", SimpleNamespace(mods_dir=mods)):
                present, missing = downloader.partition_present({GUID_A, GUID_B})
        self.assertEqual(present, {GUID_A})
        self.assertEqual(missing, {GUID_B})

    async def test_expected_closure_degrades_to_requested_set_on_resolver_failure(self) -> None:
        ctx = FakeContext()

        async def boom(session, root_guids, **kwargs):
            raise RuntimeError("workshop API 503")

        with (
            patch.object(downloader, "SessionLocal", lambda: FakeSession()),
            patch.object(downloader, "resolve_dependencies", boom),
        ):
            expected = await downloader.expected_closure(ctx, [GUID_A.lower()])
        self.assertEqual(expected, {GUID_A})
        self.assertTrue(any("closure unresolved" in line for line in ctx.logs))

    async def test_job_retries_once_when_addon_missing_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp)
            _make_addon(mods, GUID_A)
            calls: list[list[str]] = []

            async def fake_engine(ctx, guids, versions) -> dict:
                calls.append(list(guids))
                if len(calls) > 1:  # the retry pass lands the missing addon
                    _make_addon(mods, GUID_B)
                return {
                    "guids": guids,
                    "versions": versions,
                    "progress": 100.0,
                    "speed": None,
                    "downloaded": {},
                }

            refreshed: list[list[str]] = []

            async def fake_refresh(guids) -> list[str]:
                refreshed.append(list(guids))
                return sorted({g.upper() for g in guids})

            ctx = FakeContext()
            with (
                patch.object(scanner, "settings", SimpleNamespace(mods_dir=mods)),
                patch.object(downloader, "SessionLocal", lambda: FakeSession()),
                patch.object(downloader, "_run_engine", fake_engine),
                patch.object(downloader, "resolve_dependencies", _fake_resolve),
                patch.object(main, "_job_params", _fake_params({"guids": [GUID_A.lower(), GUID_B]})),
                patch.object(main, "refresh_local_mods", fake_refresh),
            ):
                result = await main._job_mod_download(ctx)

        self.assertEqual(result["missing_after_retry"], [])
        self.assertEqual(calls[0], [GUID_A, GUID_B])
        self.assertEqual(calls[1], [GUID_B])
        self.assertEqual(set(refreshed[0]), {GUID_A, GUID_B})
        self.assertTrue(any("closure incomplete, retrying 1" in line for line in ctx.logs))

    async def test_job_fails_when_addons_still_missing_after_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp)
            _make_addon(mods, GUID_A)  # GUID_B never lands on disk
            calls: list[list[str]] = []

            async def fake_engine(ctx, guids, versions) -> dict:
                calls.append(list(guids))
                return {
                    "guids": guids,
                    "versions": versions,
                    "progress": 100.0,
                    "speed": None,
                    "downloaded": {},
                }

            async def fake_refresh(guids) -> list[str]:
                return sorted({g.upper() for g in guids})

            ctx = FakeContext()
            with (
                patch.object(scanner, "settings", SimpleNamespace(mods_dir=mods)),
                patch.object(downloader, "SessionLocal", lambda: FakeSession()),
                patch.object(downloader, "_run_engine", fake_engine),
                patch.object(downloader, "resolve_dependencies", _fake_resolve),
                patch.object(main, "_job_params", _fake_params({"guids": [GUID_A, GUID_B]})),
                patch.object(main, "refresh_local_mods", fake_refresh),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    await main._job_mod_download(ctx)

        self.assertIn("download incomplete; missing on disk", str(cm.exception))
        self.assertIn(GUID_B, str(cm.exception))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], [GUID_B])
        self.assertTrue(any("closure incomplete, retrying 1" in line for line in ctx.logs))


if __name__ == "__main__":
    unittest.main()
