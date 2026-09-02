"""Fixture coverage for Reforger startup diagnosis."""

from __future__ import annotations

import json
from pathlib import Path

from app.servers.diagnosis import diagnose, extract_addon_guids


FIXTURES = Path(__file__).resolve().parents[2] / "docs" / "log-fixtures"


def test_all_log_fixtures_are_readable_and_json_safe() -> None:
    for fixture in FIXTURES.glob("*.log"):
        result = diagnose(fixture)
        if result is not None:
            json.dumps(result.as_dict())


def test_fixture_7_reports_script_compilation_and_loading_set() -> None:
    result = diagnose(FIXTURES / "REFORGER_7.log")
    assert result is not None
    assert result.kind == "script_compilation_failed"
    assert "6294F6D5EDD5CA66" in result.implicated_guids
    assert result.script_errors


def test_fixture_8_reports_deleted_dependencies() -> None:
    result = diagnose(FIXTURES / "REFORGER_8.log")
    assert result is not None
    assert result.kind == "dependencies_deleted"
    assert result.addon_count == 6
    assert "65AC6E702A3FA981" in result.implicated_guids


def test_fixture_9_reports_blocked_addon_and_missing_dependencies() -> None:
    result = diagnose(FIXTURES / "REFORGER_9.log")
    assert result is not None
    assert result.kind == "addon_blocked"
    assert result.addon_count == 5
    assert result.implicated_guids == [
        "658756C5760E94DE", "6512CC017515F9EB", "65906C6513A8D3D4",
        "64869009DD4637C4", "64863EE1C8CF7512",
    ]


def test_healthy_tail_and_steamcmd_log_have_no_reforger_startup_diagnosis() -> None:
    assert diagnose(FIXTURES / "REFORGER_13_tail.log") is None
    assert diagnose(FIXTURES / "steamcmd_latest.log") is None


def test_success_marker_and_failed_init_are_distinguished() -> None:
    success = diagnose("NETWORK      : Starting RPL server, listening on 0.0.0.0:2001")
    failed = diagnose("ENGINE       : Game destroyed.")
    assert success is not None and success.success is True
    assert failed is not None and failed.kind == "failed_initialization"


def test_addon_guid_helper_includes_loading_failure_guids() -> None:
    assert extract_addon_guids("ENGINE    (E): Addon loading failed {AAAAAAAAAAAAAAAA,BBBBBBBBBBBBBBBB}") == [
        "AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB"
    ]


def test_standalone_addon_loading_failure_is_diagnosed() -> None:
    result = diagnose("ENGINE    (E): Addon loading failed {AAAAAAAAAAAAAAAA,BBBBBBBBBBBBBBBB}")
    assert result is not None
    assert result.kind == "addon_loading_failed"


def test_remaining_known_fatal_patterns_are_diagnosed() -> None:
    cases = {
        "BACKEND (E): Addon AAAAAAAAAAAAAAAA - Addon was not found on workshop.": "addon_not_found",
        "BACKEND (E): 2 addons are not downloadable! Cannot start until they are removed from server config.": "addons_not_downloadable",
        "SCRIPT (E): Can't find class MissingType": "script_errors",
        "ENGINE (E): Cannot create game!": "cannot_create_game",
        "ENGINE (E): Unable to initialize the game": "unable_to_initialize",
    }
    for log_text, kind in cases.items():
        result = diagnose(log_text)
        assert result is not None
        assert result.kind == kind
