"""Read-only helpers for a server's Reforger ``console.log``.

This module intentionally does not participate in the supervisor's live tail.
API and WebSocket wiring can use these helpers to render stored log content
without changing the running process or its files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from ..core.app_settings import get_spam_patterns
from ..core.config import settings

Severity = Literal["debug", "info", "warning", "error"]

DEFAULT_SEARCH_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_SEARCH_MAX_RESULTS = 100

_SEVERITY_CODES: dict[str, Severity] = {
    "D": "debug",
    "I": "info",
    "W": "warning",
    "E": "error",
}
_SEVERITY_ALIASES: dict[str, Severity] = {
    "debug": "debug",
    "d": "debug",
    "info": "info",
    "i": "info",
    "warning": "warning",
    "warn": "warning",
    "w": "warning",
    "error": "error",
    "err": "error",
    "e": "error",
}
# Reforger writes lines such as ``ENGINE (E): ...`` and ``SCRIPT (W): ...``.
_SEVERITY_RE = re.compile(r"\(([DIWE])\)\s*:", re.IGNORECASE)


@dataclass(frozen=True)
class LogFile:
    """Resolved console-log metadata; ``exists`` is false for a missing log."""

    server_id: int
    path: Path
    exists: bool
    size: int = 0
    modified_at: datetime | None = None


@dataclass(frozen=True)
class LogLine:
    line_number: int
    text: str
    severity: Severity | None
    is_spam: bool


@dataclass(frozen=True)
class LogSearchResult:
    file: LogFile
    query: str
    matches: list[LogLine] = field(default_factory=list)
    scanned_bytes: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class LogDownload:
    """Raw console bytes and HTTP-friendly metadata for a download response."""

    file: LogFile
    content: bytes
    content_type: str = "text/plain; charset=utf-8"
    filename: str = "console.log"


def _normalise_server_id(server_id: int) -> int:
    if isinstance(server_id, bool) or not isinstance(server_id, int) or server_id <= 0:
        raise ValueError("server_id must be a positive integer")
    return server_id


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def current_log(server_id: int) -> LogFile:
    """Return metadata for the current server console log without creating it.

    The fixed ``logs/console.log`` path is resolved and required to remain under
    the server's log directory, preventing a malicious symlink from exposing an
    arbitrary host file through a later API download endpoint.
    """
    server_id = _normalise_server_id(server_id)
    profiles_root = Path(settings.profiles_dir).resolve(strict=False)
    profile_dir = settings.profile_dir(server_id)
    resolved_profile_dir = profile_dir.resolve(strict=False)
    logs_dir = profile_dir / "logs"
    resolved_logs_dir = logs_dir.resolve(strict=False)
    candidate = logs_dir / "console.log"
    resolved_candidate = candidate.resolve(strict=False)
    if (
        not _is_relative_to(resolved_profile_dir, profiles_root)
        or not _is_relative_to(resolved_logs_dir, resolved_profile_dir)
        or not _is_relative_to(resolved_candidate, resolved_logs_dir)
    ):
        return LogFile(server_id=server_id, path=candidate, exists=False)

    try:
        stat = resolved_candidate.stat()
    except OSError:
        return LogFile(server_id=server_id, path=resolved_candidate, exists=False)
    if not resolved_candidate.is_file():
        return LogFile(server_id=server_id, path=resolved_candidate, exists=False)
    return LogFile(
        server_id=server_id,
        path=resolved_candidate,
        exists=True,
        size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def normalise_severity(value: str | None) -> Severity | None:
    """Map API-friendly severity names and Reforger's one-letter codes."""
    if value is None:
        return None
    try:
        return _SEVERITY_ALIASES[value.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"unknown severity: {value!r}") from exc


def line_severity(text: str) -> Severity | None:
    """Extract a Reforger severity from e.g. ``ENGINE (E):``."""
    match = _SEVERITY_RE.search(text)
    return _SEVERITY_CODES[match.group(1).upper()] if match else None


def is_spam_line(text: str) -> bool:
    """Whether a line matches a configured spam pattern (settings-backed).

    Patterns are lowercased substrings compared against the lowercased line;
    with a cold cache this falls back to the historical built-in default.
    """
    lowered = text.lower()
    return any(pattern in lowered for pattern in get_spam_patterns())


def iter_log_lines(
    server_id: int,
    *,
    severity: str | None = None,
    hide_spam: bool = True,
) -> Iterator[LogLine]:
    """Stream matching log lines without loading the log into memory.

    A missing log simply produces no lines. Lines without a Reforger severity
    are included only when no severity filter is requested.
    """
    wanted = normalise_severity(severity)
    log = current_log(server_id)
    if not log.exists:
        return

    try:
        with log.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, raw in enumerate(handle, start=1):
                text = raw.rstrip("\r\n")
                detected = line_severity(text)
                spam = is_spam_line(text)
                if (wanted is not None and detected != wanted) or (hide_spam and spam):
                    continue
                yield LogLine(line_number, text, detected, spam)
    except OSError:
        return


def search_log(
    server_id: int,
    query: str,
    *,
    severity: str | None = None,
    hide_spam: bool = True,
    max_bytes: int = DEFAULT_SEARCH_MAX_BYTES,
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS,
) -> LogSearchResult:
    """Case-insensitively search a bounded prefix of the current console log."""
    if not query:
        raise ValueError("query must not be empty")
    if max_bytes < 1 or max_results < 1:
        raise ValueError("max_bytes and max_results must be positive")

    wanted = normalise_severity(severity)
    log = current_log(server_id)
    result = LogSearchResult(file=log, query=query)
    if not log.exists:
        return result

    matches: list[LogLine] = []
    scanned_bytes = 0
    line_number = 0
    needle = query.casefold()
    try:
        with log.path.open("rb") as handle:
            while scanned_bytes < max_bytes and len(matches) < max_results:
                raw = handle.readline(max_bytes - scanned_bytes)
                if not raw:
                    break
                scanned_bytes += len(raw)
                line_number += 1
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                detected = line_severity(text)
                spam = is_spam_line(text)
                if needle not in text.casefold():
                    continue
                if (wanted is not None and detected != wanted) or (hide_spam and spam):
                    continue
                matches.append(LogLine(line_number, text, detected, spam))
    except OSError:
        return result

    truncated = scanned_bytes < log.size and (
        scanned_bytes >= max_bytes or len(matches) >= max_results
    )
    return LogSearchResult(log, query, matches, scanned_bytes, truncated)


def read_log_download(server_id: int) -> LogDownload | None:
    """Read the raw log bytes for a download endpoint, or ``None`` if absent."""
    log = current_log(server_id)
    if not log.exists:
        return None
    try:
        return LogDownload(file=log, content=log.path.read_bytes())
    except OSError:
        return None
