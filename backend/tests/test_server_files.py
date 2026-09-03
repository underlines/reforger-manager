from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import settings
from app.servers.files import (
    UnsafePath,
    list_dir,
    resolve_safe_path,
)


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "profiles_dir", tmp_path)
    return tmp_path


@pytest.fixture
def profile7(profiles_dir: Path) -> Path:
    p = profiles_dir / "7"
    p.mkdir()
    return p


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError as exc:  # Windows without Developer Mode / admin
        pytest.skip(f"symlinks unavailable on this host: {exc}")


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "..",
        "foo/../bar",
        "with\x00null",
        "logs/console.log",
        "addons_tmp/x",
    ],
)
def test_resolve_rejects_unsafe_paths(profile7: Path, bad: str) -> None:
    with pytest.raises(UnsafePath):
        resolve_safe_path(7, bad)


def test_resolve_allows_root_and_nested(profile7: Path) -> None:
    assert resolve_safe_path(7, None) == profile7.resolve()
    assert resolve_safe_path(7, "") == profile7.resolve()
    assert resolve_safe_path(7, "PersistentXP.json") == (profile7 / "PersistentXP.json").resolve()
    assert resolve_safe_path(7, "sub/dir/f.txt") == (profile7 / "sub" / "dir" / "f.txt").resolve()


def test_resolve_is_scoped_per_server_and_never_crosses(profiles_dir: Path) -> None:
    (profiles_dir / "7").mkdir()
    (profiles_dir / "8").mkdir()
    p7 = resolve_safe_path(7, "GMPersistentLoadouts/a.json")
    p8 = resolve_safe_path(8, "GMPersistentLoadouts/a.json")
    assert p7 != p8
    assert p7.parent.parent.name == "7"
    assert p8.parent.parent.name == "8"
    assert (profiles_dir / "8") not in p7.parents
    assert (profiles_dir / "7") not in p8.parents


def test_resolve_rejects_symlink_pointing_outside(profile7: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(profile7 / "escape", outside)
    with pytest.raises(UnsafePath):
        resolve_safe_path(7, "escape/secret.txt")


def test_resolve_rejects_symlink_pointing_inside(profile7: Path) -> None:
    real = profile7 / "real"
    real.mkdir()
    _symlink_or_skip(profile7 / "link", real)
    with pytest.raises(UnsafePath):
        resolve_safe_path(7, "link")
    with pytest.raises(UnsafePath):
        resolve_safe_path(7, "link/f.txt")


def test_list_dir_missing_profile_returns_empty(profiles_dir: Path) -> None:
    assert list_dir(99, None) == []
    assert list_dir(99, "sub") == []


def test_list_dir_orders_dirs_first_excludes_reserved_and_lists_symlinks(profile7: Path) -> None:
    (profile7 / "logs").mkdir()
    (profile7 / "addons_tmp").mkdir()
    (profile7 / "Bravo").mkdir()
    (profile7 / "alpha").mkdir()
    (profile7 / "notes.txt").write_text("hi", encoding="utf-8")
    (profile7 / "PersistentXP.json").write_text("{}", encoding="utf-8")
    _symlink_or_skip(profile7 / "zlink", profile7 / "notes.txt")

    entries = list_dir(7, None)
    names = [e.name for e in entries]

    assert "logs" not in names and "addons_tmp" not in names
    assert names[:2] == ["alpha", "Bravo"]  # dirs first, case-insensitive
    assert names[2:] == ["notes.txt", "PersistentXP.json", "zlink"]
    assert next(e for e in entries if e.name == "zlink").type == "symlink"
    assert next(e for e in entries if e.name == "PersistentXP.json").is_json is True


def test_is_json_false_for_syntactically_broken_json(profile7: Path) -> None:
    (profile7 / "broken.json").write_text('{"a": 1,,}', encoding="utf-8")
    entry = next(e for e in list_dir(7, None) if e.name == "broken.json")
    assert entry.is_text is True
    assert entry.is_json is False


def test_is_text_false_for_bytes_with_nul(profile7: Path) -> None:
    (profile7 / "blob.bin").write_bytes(b"PK\x03\x04\x00\x00binary")
    entry = next(e for e in list_dir(7, None) if e.name == "blob.bin")
    assert entry.is_text is False
    assert entry.is_json is False


def test_is_text_false_for_file_over_edit_cap(profile7: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "files_max_edit_bytes", 8)
    (profile7 / "big.json").write_text('{"key": "value that exceeds the cap"}', encoding="utf-8")
    entry = next(e for e in list_dir(7, None) if e.name == "big.json")
    assert entry.is_text is False
    assert entry.is_json is False
