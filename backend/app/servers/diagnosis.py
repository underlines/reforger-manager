"""Known Reforger startup failure diagnosis from console logs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re


GUID = r"[0-9A-F]{16}"
SUCCESS_RE = re.compile(
    r"NETWORK\s+: Starting RPL server, listening on 0\.0\.0\.0:2001"
)
BLOCKED_RE = re.compile(rf"Addon ({GUID}) \((.+?)\) - Addon is blocked\.")
NOT_FOUND_RE = re.compile(rf"Addon ({GUID}) - Addon was not found on workshop\.")
DEPS_DELETED_RE = re.compile(
    rf"Addon ({GUID}) \((.+?)\) - Addon has dependencies deleted from workshop\."
)
NOT_DOWNLOADABLE_RE = re.compile(
    r"(\d+) addons are not downloadable! Cannot start until they are removed from server config\."
)
LOADING_FAILED_RE = re.compile(rf"ENGINE\s+\(E\): Addon loading failed \{{({GUID}(?:,{GUID})*)\}}")
SCRIPT_ERROR_RE = re.compile(
    r"SCRIPT\s+\(E\): .*?(?:Can't find variable|Can't find class|Too many parameters|Syntax error)"
)


@dataclass(frozen=True)
class Diagnosis:
    """A compact, API/event-safe description of a recognized log outcome."""

    cause: str
    implicated_guids: list[str]
    suggested_action: str
    success: bool
    kind: str
    addon_count: int | None = None
    script_errors: list[str] | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def extract_addon_guids(log_text_or_path: str | Path) -> list[str]:
    """Return unique addon GUIDs reported by recognized addon log messages."""
    text = _read_log(log_text_or_path)
    guids: list[str] = []
    for pattern in (BLOCKED_RE, NOT_FOUND_RE, DEPS_DELETED_RE):
        guids.extend(match.group(1) for match in pattern.finditer(text))
    for match in LOADING_FAILED_RE.finditer(text):
        guids.extend(match.group(1).split(","))
    return _unique(guids)


def diagnose(log_text_or_path: str | Path) -> Diagnosis | None:
    """Diagnose the most actionable known Reforger log outcome, if any."""
    text = _read_log(log_text_or_path)
    if SUCCESS_RE.search(text):
        return Diagnosis(
            cause="Server started successfully and is accepting RPL connections.",
            implicated_guids=[],
            suggested_action="No action required.",
            success=True,
            kind="started",
        )

    blocked = list(BLOCKED_RE.finditer(text))
    not_found = list(NOT_FOUND_RE.finditer(text))
    deps_deleted = list(DEPS_DELETED_RE.finditer(text))
    addon_count_match = NOT_DOWNLOADABLE_RE.search(text)
    addon_count = int(addon_count_match.group(1)) if addon_count_match else None
    addon_guids = extract_addon_guids(text)

    if blocked:
        names = _unique(match.group(2) for match in blocked)
        return Diagnosis(
            cause=f"Workshop addon{'s' if len(blocked) > 1 else ''} blocked: {', '.join(names)}.",
            implicated_guids=addon_guids,
            suggested_action="Remove or replace the blocked addon and its unavailable dependencies in the server configuration.",
            success=False,
            kind="addon_blocked",
            addon_count=addon_count,
        )
    if deps_deleted:
        names = _unique(match.group(2) for match in deps_deleted)
        return Diagnosis(
            cause=f"Addon dependencies were deleted from the Workshop: {', '.join(names)}.",
            implicated_guids=addon_guids,
            suggested_action="Remove or replace the addon, or select a version whose dependencies are still available.",
            success=False,
            kind="dependencies_deleted",
            addon_count=addon_count,
        )
    if not_found:
        return Diagnosis(
            cause="One or more addons were not found on the Workshop.",
            implicated_guids=addon_guids,
            suggested_action="Remove or replace the missing addons and dependencies in the server configuration.",
            success=False,
            kind="addon_not_found",
            addon_count=addon_count,
        )
    if addon_count is not None:
        return Diagnosis(
            cause=f"{addon_count} addon{'s are' if addon_count != 1 else ' is'} not downloadable.",
            implicated_guids=addon_guids,
            suggested_action="Remove or replace the unavailable addons in the server configuration.",
            success=False,
            kind="addons_not_downloadable",
            addon_count=addon_count,
        )

    script_errors = [match.group(0).strip() for match in SCRIPT_ERROR_RE.finditer(text)]
    if 'Can\'t compile "Game" script module!' in text:
        return Diagnosis(
            cause="Addon scripts do not compile against the installed Reforger engine build.",
            implicated_guids=addon_guids,
            suggested_action="Update, pin to a compatible version, or remove the implicated addon(s).",
            success=False,
            kind="script_compilation_failed",
            script_errors=script_errors or None,
        )
    if script_errors:
        return Diagnosis(
            cause="Addon script compatibility errors prevented game initialization.",
            implicated_guids=addon_guids,
            suggested_action="Update, pin to a compatible version, or remove the implicated addon(s).",
            success=False,
            kind="script_errors",
            script_errors=script_errors,
        )
    if LOADING_FAILED_RE.search(text):
        return Diagnosis(
            cause="The engine could not load the listed addon set.",
            implicated_guids=addon_guids,
            suggested_action="Review the listed addons and their dependencies for compatibility or availability problems.",
            success=False,
            kind="addon_loading_failed",
        )
    if re.search(r"ENGINE\s+\(E\): Cannot create game!", text):
        return _engine_failure("cannot_create_game", "The engine could not create the game world.", addon_guids)
    if re.search(r"ENGINE\s+\(E\): Unable to initialize the game", text):
        return _engine_failure("unable_to_initialize", "The engine was unable to initialize the game.", addon_guids)
    if re.search(r"ENGINE\s+: Game destroyed\.", text) and not _shows_running_game(text):
        return _engine_failure("failed_initialization", "The server exited during initialization before RPL started.", addon_guids)
    if "Failed to fetch addon details from workshop API!" in text:
        return Diagnosis(
            cause="The Workshop API could not provide addon details.",
            implicated_guids=addon_guids,
            suggested_action="Retry later; if the problem persists, replace the affected addons.",
            success=False,
            kind="workshop_api_failure",
        )
    return None


def _engine_failure(kind: str, cause: str, guids: list[str]) -> Diagnosis:
    return Diagnosis(
        cause=cause,
        implicated_guids=guids,
        suggested_action="Review the preceding addon and script errors, then update, replace, or remove the affected addon.",
        success=False,
        kind=kind,
    )


def _read_log(log_text_or_path: str | Path) -> str:
    if isinstance(log_text_or_path, Path):
        return log_text_or_path.read_text(encoding="utf-8", errors="replace")
    if "\n" not in log_text_or_path and "\r" not in log_text_or_path:
        try:
            path = Path(log_text_or_path)
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return log_text_or_path


def _shows_running_game(text: str) -> bool:
    """Avoid calling a normal shutdown in a truncated healthy log a failed start."""
    return "[PERSISTENCE] Save" in text or "WORLD        :" in text


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
