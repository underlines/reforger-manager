from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.mods import downloader


class DownloadProgressTests(unittest.TestCase):
    def test_parses_verified_lines_and_weights_known_sizes(self):
        progress = downloader.DownloadProgress()
        self.assertEqual(
            progress.feed("BACKEND : Addon Download started ABCDEF0123456789 - Small Mod"),
            {"kind": "start", "guid": "ABCDEF0123456789", "name": "Small Mod"},
        )
        progress.feed("BACKEND : Addon Download started 0123456789ABCDEF - Large Mod")
        progress.feed("BACKEND : Small Mod: [====>___] 20% 20/100 MB")
        progress.feed("BACKEND : Large Mod: [====>___] 50% 500/1000 MB")
        self.assertAlmostEqual(
            progress.percent(["ABCDEF0123456789", "0123456789ABCDEF"]),
            47.272727,
            places=5,
        )
        self.assertEqual(
            progress.feed("BACKEND : Download speed 1.64 KB/s"),
            {"kind": "speed", "speed": "1.64 KB/s"},
        )
        self.assertEqual(
            progress.feed("BACKEND : Required addons are ready to use."),
            {"kind": "ready"},
        )
        self.assertTrue(progress.ready)

    def test_builds_minimal_pinned_config_and_confirmed_command(self):
        guids, versions = downloader._normalise_inputs(
            ["abcdef0123456789"], {"abcdef0123456789": "1.2.3"}
        )
        config = downloader._build_config(guids, versions)
        self.assertEqual(
            config["game"]["mods"],
            [{"modId": "ABCDEF0123456789", "version": "1.2.3"}],
        )
        # The engine schema-validates the config and rejects an empty
        # scenarioId; the stub must carry a well-formed placeholder.
        self.assertRegex(
            config["game"]["scenarioId"],
            r"^\{[0-9A-F]{16}\}[a-zA-Z0-9_./ -]+$",
        )
        command = downloader._build_command(
            Path("/tmp/config.json"), Path("/tmp/profile"), Path("/tmp/addons")
        )
        self.assertEqual(command[1], "-config")
        self.assertEqual(command[3], "-profile")
        self.assertEqual(command[5], "-addonDownloadDir")
        # must include the "reforger" segment so downloads land under
        # scanner.addons_root() == <mods_dir>/reforger/addons
        self.assertTrue(command[6].replace("\\", "/").endswith("/reforger"), command[6])
        self.assertEqual(command[7], "-addonTempDir")
        self.assertEqual(command[9], "-nothrow")
        self.assertEqual(command[-2:], ["-maxFPS", "10"])

    def test_rejects_invalid_or_unrequested_pins(self):
        with self.assertRaises(ValueError):
            downloader._normalise_inputs(["not-a-guid"], None)
        with self.assertRaises(ValueError):
            downloader._normalise_inputs(
                ["ABCDEF0123456789"], {"0123456789ABCDEF": "1"}
            )


class _FakeProcess:
    """Stands in for the headless engine. ``_run_engine`` now follows the engine's
    on-disk ``console.log`` (block-buffered stdout used to make the job hang), so
    the fake only needs process-control surface, not a stdout stream."""

    def __init__(self):
        self.returncode = None
        self.signals = []
        self.killed = False

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = -value

    def terminate(self):
        self.returncode = self.returncode or -downloader.signal.SIGTERM

    def kill(self):
        self.killed = True
        self.returncode = self.returncode or -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class DownloadRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _log_with(self, *lines: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="e2e-dl-test-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        log = tmp / "console.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return log

    async def test_ready_marker_in_the_engine_log_is_success_and_terminates(self):
        process = _FakeProcess()
        log = self._log_with(
            "18:00:00.001 BACKEND      : Addon Download started ABCDEF0123456789 - Example",
            "18:00:01.002 BACKEND      : Example: [====>___] 20% 20/100 MB",
            "18:00:02.003 BACKEND      : Required addons are ready to use.",
            "18:00:02.100 ENGINE       : Game destroyed.",
        )
        with patch("app.mods.downloader.Path.is_file", return_value=True), patch(
            "app.mods.downloader.asyncio.create_subprocess_exec", return_value=process
        ), patch("app.mods.downloader._newest_console_log", new=AsyncMock(return_value=log)):
            result = await downloader._run_engine(None, ["ABCDEF0123456789"], {})
        self.assertEqual(result["progress"], 100.0)
        self.assertIn(downloader.signal.SIGTERM, process.signals)

    async def test_fatal_line_in_the_engine_log_raises_with_a_diagnosis(self):
        process = _FakeProcess()
        log = self._log_with(
            "18:00:00 BACKEND      : Addon Download started ABCDEF0123456789 - Example",
            "18:00:01 BACKEND      : Addon ABCDEF0123456789 v1 - Addon is blocked.",
        )
        with patch("app.mods.downloader.Path.is_file", return_value=True), patch(
            "app.mods.downloader.asyncio.create_subprocess_exec", return_value=process
        ), patch("app.mods.downloader._newest_console_log", new=AsyncMock(return_value=log)):
            with self.assertRaises(downloader.ModDownloadError):
                await downloader._run_engine(None, ["ABCDEF0123456789"], {})
        self.assertIn(downloader.signal.SIGTERM, process.signals)

    async def test_engine_exit_without_ready_marker_raises(self):
        process = _FakeProcess()
        process.returncode = 1  # already gone, log never showed the ready marker
        log = self._log_with("18:00:00 ENGINE       : Initializing engine, version 191843")
        with patch("app.mods.downloader.Path.is_file", return_value=True), patch(
            "app.mods.downloader.asyncio.create_subprocess_exec", return_value=process
        ), patch("app.mods.downloader._newest_console_log", new=AsyncMock(return_value=log)), patch(
            "app.mods.downloader._POST_EXIT_GRACE", 0.05
        ):
            with self.assertRaises(downloader.ModDownloadError):
                await downloader._run_engine(None, ["ABCDEF0123456789"], {})
