"""Runtime settings service: the ``app_settings`` singleton (row id = 1) and the
in-process spam-pattern cache consumed by :func:`app.mods.logview.is_spam_line`.

The spam cache exists because ``is_spam_line`` is synchronous (it filters the
console WebSocket loop and the stored-log stream) and therefore cannot query the
async DB. It is warmed whenever the settings row is loaded through
:func:`get_app_settings` (startup, GET/PATCH /api/settings) and invalidated on
PATCH; when cold, :func:`get_spam_patterns` falls back to the historical
built-in default so a cold cache behaves exactly like the old module constant.
"""

from __future__ import annotations

import threading
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import APP_SETTINGS_SINGLETON_ID, AppSettings
from .config import settings

# The pattern list the code shipped with before it became configurable
# (logview._SPAM_PATTERNS): lowercase, matched against a lowercased line.
DEFAULT_SPAM_PATTERNS: tuple[str, ...] = ("thermalprofiledefault.conf",)

_spam_cache: tuple[str, ...] | None = None
_spam_cache_lock = threading.Lock()


def get_spam_patterns() -> tuple[str, ...]:
    """Current lowercase spam patterns; the built-in default when cold."""
    with _spam_cache_lock:
        if _spam_cache is None:
            return DEFAULT_SPAM_PATTERNS
        return _spam_cache


def set_spam_cache(patterns: Iterable[str]) -> None:
    """Warm the cache with the given patterns (stored/matched lowercased)."""
    global _spam_cache
    lowered = tuple(pattern.lower() for pattern in patterns)
    with _spam_cache_lock:
        _spam_cache = lowered


def invalidate_spam_cache() -> None:
    """Drop the cached patterns (callers re-warm from the persisted row)."""
    global _spam_cache
    with _spam_cache_lock:
        _spam_cache = None


async def load_spam_cache(session: AsyncSession) -> None:
    """Warm the cache from the persisted row (or the default when absent)."""
    row = await session.get(AppSettings, APP_SETTINGS_SINGLETON_ID)
    set_spam_cache(row.log_spam_patterns if row is not None and row.log_spam_patterns else DEFAULT_SPAM_PATTERNS)


async def get_app_settings(session: AsyncSession) -> AppSettings:
    """Return the settings singleton, creating it lazily from env defaults.

    Side effect: refreshes the spam-pattern cache from the returned row, so any
    code path that loads settings keeps the cache coherent.
    """
    row = await session.get(AppSettings, APP_SETTINGS_SINGLETON_ID)
    if row is None:
        row = AppSettings(
            id=APP_SETTINGS_SINGLETON_ID,
            nightly_check_enabled=settings.nightly_check_enabled,
            nightly_check_hour=settings.nightly_check_hour,
            log_spam_patterns=list(DEFAULT_SPAM_PATTERNS),
        )
        session.add(row)
        await session.flush()
    set_spam_cache(row.log_spam_patterns if row.log_spam_patterns else DEFAULT_SPAM_PATTERNS)
    return row
