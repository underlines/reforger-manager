"""Deterministic tests for the verify/repair job; no Reforger binary is run."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.mods import verify  # noqa: E402


class FakeContext:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.progress_updates: list[tuple[float, str | None]] = []
        self.cancelled = False

    async def log(self, line: str) -> None:
        self.logs.append(line)

    async def progress(self, pct: float, step: str | None = None) -> None:
        self.progress_updates.append((pct, step))


class FakeProcess:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = self._lines(lines)
        self.returncode = None
        self._exit_code = returncode
        self.terminated = False

    async def _lines(self, lines: list[str]):
        for line in lines:
            yield line.encode()

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class VerifyRepairTests(unittest.IsolatedAsyncioTestCase):
    def test_build_args_uses_confirmed_flags(self) -> None:
        fake_settings = SimpleNamespace(
            reforger_binary=Path("/srv/ArmaReforgerServer"), mods_dir=Path("/data/mods")
        )
        with patch.object(verify, "settings", fake_settings):
            self.assertEqual(
                verify._build_args(),
                [
                    str(Path("/srv/ArmaReforgerServer")), "-addonsDir", str(Path("/data/mods")),
                    "-addonsVerify", "-addonsRepair", "-nothrow",
                ],
            )

    def test_output_parser_tracks_only_explicit_per_addon_outcomes(self) -> None:
        progress = verify._VerifyProgress()
        progress.consume("Addon 0123456789ABCDEF verified")
        progress.consume("Addon FEDCBA9876543210 repaired")
        progress.consume("Addon 1111111111111111 verification failed")
        progress.consume("Verified addon 2222222222222222")
        progress.consume("Verification complete")
        self.assertEqual(progress.result(), {"checked": 3, "repaired": 1, "failed": 1})

    async def test_job_streams_output_and_logs_unfiltered_guid_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "ArmaReforgerServer"
            binary.touch()
            fake_settings = SimpleNamespace(
                reforger_binary=binary, mods_dir=root / "mods", server_dir=root
            )
            process = FakeProcess([
                "Addon 0123456789ABCDEF verified\n",
                "Addon FEDCBA9876543210 repaired\n",
            ])
            ctx = FakeContext()
            with (
                patch.object(verify, "settings", fake_settings),
                patch.object(verify.supervisor, "is_running", return_value=False),
                patch("app.mods.verify.asyncio.create_subprocess_exec", return_value=process) as spawn,
            ):
                result = await verify.run_verify_repair(ctx, ["0123456789abcdef"])

        self.assertEqual(result, {"checked": 2, "repaired": 1, "failed": 0})
        self.assertIn("-addonsVerify", spawn.await_args.args)
        self.assertNotIn("0123456789abcdef", spawn.await_args.args)
        self.assertTrue(any("entire addon cache" in line for line in ctx.logs))

    async def test_job_refuses_while_server_is_running(self) -> None:
        ctx = FakeContext()
        with patch.object(verify.supervisor, "is_running", return_value=True), patch.object(
            type(verify.supervisor), "active_server_id", new_callable=PropertyMock, return_value=7
        ):
            with self.assertRaisesRegex(verify.VerifyRepairError, "server 7 is running"):
                await verify.run_verify_repair(ctx)

    async def test_fatal_output_raises_meaningful_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "ArmaReforgerServer"
            binary.touch()
            fake_settings = SimpleNamespace(
                reforger_binary=binary, mods_dir=root / "mods", server_dir=root
            )
            process = FakeProcess(["ENGINE (E): Unable to initialize the game\n"])
            with (
                patch.object(verify, "settings", fake_settings),
                patch.object(verify.supervisor, "is_running", return_value=False),
                patch("app.mods.verify.asyncio.create_subprocess_exec", return_value=process),
            ):
                with self.assertRaisesRegex(verify.VerifyRepairError, "fatal addon verification diagnosis"):
                    await verify.run_verify_repair(FakeContext())
            self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
