from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.mods.logview import (
    current_log,
    iter_log_lines,
    read_log_download,
    search_log,
)


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "profiles_dir", tmp_path)
    return tmp_path


@pytest.fixture
def console_log(profiles_dir: Path) -> Path:
    path = profiles_dir / "7" / "logs" / "console.log"
    path.parent.mkdir(parents=True)
    path.write_text(
        "ENGINE (I): Booting server\n"
        "SCRIPT (W): Deprecated function\n"
        "ENGINE (E): Cannot create game!\n"
        "ENGINE (E): Failed loading thermalProfileDefault.conf\n"
        "plain unclassified line\n",
        encoding="utf-8",
    )
    return path


def test_iter_log_lines_filters_reforger_severity_and_spam(console_log: Path) -> None:
    errors = list(iter_log_lines(7, severity="error"))

    assert [line.text for line in errors] == ["ENGINE (E): Cannot create game!"]
    assert errors[0].severity == "error"

    warnings = list(iter_log_lines(7, severity="W"))
    assert [line.line_number for line in warnings] == [2]


def test_spam_is_hidden_by_default_and_can_be_included(console_log: Path) -> None:
    default_lines = list(iter_log_lines(7))
    all_lines = list(iter_log_lines(7, hide_spam=False))

    assert len(default_lines) == 4
    assert default_lines[-1].text == "plain unclassified line"
    assert all_lines[3].is_spam is True


def test_search_is_case_insensitive_and_bounded(console_log: Path) -> None:
    found = search_log(7, "create GAME")
    hidden = search_log(7, "thermalprofile")
    shown = search_log(7, "thermalprofile", hide_spam=False)
    limited = search_log(7, "ENGINE", max_results=1)

    assert [line.line_number for line in found.matches] == [3]
    assert hidden.matches == []
    assert [line.line_number for line in shown.matches] == [4]
    assert len(limited.matches) == 1
    assert limited.truncated is True


def test_missing_log_has_empty_read_results(profiles_dir: Path) -> None:
    metadata = current_log(99)

    assert metadata.exists is False
    assert list(iter_log_lines(99)) == []
    assert search_log(99, "anything").matches == []
    assert read_log_download(99) is None


def test_download_returns_raw_bytes_and_metadata(console_log: Path) -> None:
    download = read_log_download(7)

    assert download is not None
    assert download.content == console_log.read_bytes()
    assert download.file.size == len(download.content)
    assert download.content_type == "text/plain; charset=utf-8"
