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

# The 31 official scenarios shipped with the base game as of engine 1.6.0 (current
# through 1.8 — https://community.bistudio.com/wiki/Arma_Reforger:Server_Config
# lists no newer "Official Scenarios" revision), offered by the ScenarioField
# picker regardless of a definition's mods. Each is a {game_id, name} pair; the
# operator-facing list is fully editable afterward via PATCH /api/settings.
DEFAULT_SCENARIOS: tuple[dict[str, str], ...] = (
    {"game_id": "{ECC61978EDCC2B5A}Missions/23_Campaign.conf", "name": "Conflict - Everon"},
    {"game_id": "{C41618FD18E9D714}Missions/23_Campaign_Arland.conf", "name": "Conflict - Arland"},
    {
        "game_id": "{C700DB41F0C546E1}Missions/23_Campaign_NorthCentral.conf",
        "name": "Conflict - Northern Everon",
    },
    {
        "game_id": "{28802845ADA64D52}Missions/23_Campaign_SWCoast.conf",
        "name": "Conflict - Southern Everon",
    },
    {
        "game_id": "{94992A3D7CE4FF8A}Missions/23_Campaign_Western.conf",
        "name": "Conflict - Western Everon",
    },
    {
        "game_id": "{FDE33AFE2ED7875B}Missions/23_Campaign_Montignac.conf",
        "name": "Conflict - Montignac",
    },
    {
        "game_id": "{0220741028718E7F}Missions/23_Campaign_HQC_Everon.conf",
        "name": "Conflict: HQ Commander - Everon",
    },
    {
        "game_id": "{68D1240A11492545}Missions/23_Campaign_HQC_Arland.conf",
        "name": "Conflict: HQ Commander - Arland",
    },
    {
        "game_id": "{BB5345C22DD2B655}Missions/23_Campaign_HQC_Cain.conf",
        "name": "Conflict: HQ Commander - Kolguyev",
    },
    {
        "game_id": "{DFAC5FABD11F2390}Missions/26_CombatOpsEveron.conf",
        "name": "Combat Ops - Everon",
    },
    {"game_id": "{DAA03C6E6099D50F}Missions/24_CombatOps.conf", "name": "Combat Ops - Arland"},
    {"game_id": "{CB347F2F10065C9C}Missions/CombatOpsCain.conf", "name": "Combat Ops - Kolguyev"},
    {"game_id": "{3F2E005F43DBD2F8}Missions/CAH_Briars_Coast.conf", "name": "Capture & Hold - Briars"},
    {
        "game_id": "{F1A1BEA67132113E}Missions/CAH_Castle.conf",
        "name": "Capture & Hold - Montfort Castle",
    },
    {
        "game_id": "{589945FB9FA7B97D}Missions/CAH_Concrete_Plant.conf",
        "name": "Capture & Hold - Concrete Plant",
    },
    {
        "game_id": "{9405201CBD22A30C}Missions/CAH_Factory.conf",
        "name": "Capture & Hold - Almara Factory",
    },
    {"game_id": "{1CD06B409C6FAE56}Missions/CAH_Forest.conf", "name": "Capture & Hold - Simon's Wood"},
    {"game_id": "{7C491B1FCC0FF0E1}Missions/CAH_LeMoule.conf", "name": "Capture & Hold - Le Moule"},
    {
        "game_id": "{6EA2E454519E5869}Missions/CAH_Military_Base.conf",
        "name": "Capture & Hold - Camp Blake",
    },
    {"game_id": "{2B4183DF23E88249}Missions/CAH_Morton.conf", "name": "Capture & Hold - Morton"},
    {"game_id": "{59AD59368755F41A}Missions/21_GM_Eden.conf", "name": "Game Master - Everon"},
    {"game_id": "{2BBBE828037C6F4B}Missions/22_GM_Arland.conf", "name": "Game Master - Arland"},
    {"game_id": "{F45C6C15D31252E6}Missions/27_GM_Cain.conf", "name": "Game Master - Kolguyev"},
    {"game_id": "{C47A1A6245A13B26}Missions/SP01_ReginaV2.conf", "name": "Elimination"},
    {"game_id": "{0648CDB32D6B02B3}Missions/SP02_AirSupport.conf", "name": "Air Support"},
    {"game_id": "{002AF7323E0129AF}Missions/Tutorial.conf", "name": "Training"},
    {
        "game_id": "{10B8582BAD9F7040}Missions/Scenario01_Intro.conf",
        "name": "Operation Omega 01: Over The Hills And Far Away",
    },
    {
        "game_id": "{1D76AF6DC4DF0577}Missions/Scenario02_Steal.conf",
        "name": "Operation Omega 02: Radio Check",
    },
    {
        "game_id": "{D1647575BCEA5A05}Missions/Scenario03_Villa.conf",
        "name": "Operation Omega 03: Light In The Dark",
    },
    {
        "game_id": "{6D224A109B973DD8}Missions/Scenario04_Sabotage.conf",
        "name": "Operation Omega 04: Red Silence",
    },
    {
        "game_id": "{FA2AB0181129CB16}Missions/Scenario05_Hill.conf",
        "name": "Operation Omega 05: Cliffhanger",
    },
)

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
            default_scenarios=[dict(s) for s in DEFAULT_SCENARIOS],
        )
        session.add(row)
        await session.flush()
    elif row.default_scenarios is None:
        # The column reached this row via create_all self-heal (schema drift on
        # an install that predates it) rather than the seeding branch above —
        # backfill once so the picker default survives an upgrade untouched.
        row.default_scenarios = [dict(s) for s in DEFAULT_SCENARIOS]
        await session.flush()
    set_spam_cache(row.log_spam_patterns if row.log_spam_patterns else DEFAULT_SPAM_PATTERNS)
    return row
