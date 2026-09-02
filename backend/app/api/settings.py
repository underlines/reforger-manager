"""GET  /api/settings  -> runtime settings (nightly check, spam patterns)
PATCH /api/settings  -> partial update; restarts the nightly scheduler and
                        refreshes the spam cache as needed
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.app_settings import (
    get_app_settings,
    invalidate_spam_cache,
    load_spam_cache,
)
from ..core.db import get_session
from ..core.security import get_current_user
from ..schemas.settings import SettingsOut, SettingsUpdate

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(get_current_user)],
)


def _out(row) -> SettingsOut:
    return SettingsOut(
        nightly_check_enabled=row.nightly_check_enabled,
        nightly_check_hour=row.nightly_check_hour,
        log_spam_patterns=list(row.log_spam_patterns or []),
    )


@router.get("", response_model=SettingsOut)
async def read_settings(session: AsyncSession = Depends(get_session)) -> SettingsOut:
    row = await get_app_settings(session)
    await session.commit()  # persist the lazily seeded singleton row
    return _out(row)


@router.patch("", response_model=SettingsOut)
async def update_settings(
    body: SettingsUpdate, session: AsyncSession = Depends(get_session)
) -> SettingsOut:
    row = await get_app_settings(session)

    nightly_changed = (
        body.nightly_check_enabled is not None
        and body.nightly_check_enabled != row.nightly_check_enabled
    ) or (
        body.nightly_check_hour is not None and body.nightly_check_hour != row.nightly_check_hour
    )
    patterns_changed = body.log_spam_patterns is not None and list(body.log_spam_patterns) != list(
        row.log_spam_patterns or []
    )

    if body.nightly_check_enabled is not None:
        row.nightly_check_enabled = body.nightly_check_enabled
    if body.nightly_check_hour is not None:
        row.nightly_check_hour = body.nightly_check_hour
    if body.log_spam_patterns is not None:
        row.log_spam_patterns = list(body.log_spam_patterns)
    await session.commit()

    if patterns_changed:
        # Cold the cache, then re-warm from the persisted row: the console
        # WebSocket and stored-log readers pick the new list up immediately.
        invalidate_spam_cache()
        await load_spam_cache(session)
    if nightly_changed:
        # Local import: app.main imports this router at module level.
        from ..main import apply_nightly_settings

        await apply_nightly_settings(row.nightly_check_enabled, row.nightly_check_hour)

    return _out(row)
