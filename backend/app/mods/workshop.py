"""Async client for the unofficial Arma Reforger Workshop metadata API.

Base: ``https://api.reforgermods.net/v2`` (unofficial — Bohemia has no public
Workshop API). Everything anonymous.

Three cross-cutting concerns live here:

* **One container-wide rate limiter** (module singleton ``_LIMITER``). The API
  budget is scoped ``by client_ip`` — 60 requests/minute sustained, burst 20,
  20 000/day — so the whole process shares a single token bucket. Every request
  goes through it. On HTTP 429 the ``Retry-After`` header is honoured with a
  capped number of retries.
* **In-process TTL cache** keyed by the fully-qualified URL (path + query), so a
  disk scan that hits the same mod from ``get_mod`` / ``get_versions`` / ...
  repeatedly is cheap. ``clear_cache()`` empties it.
* **camelCase -> snake_case at the boundary.** Every dict that leaves this module
  has its keys recursively converted (``gameMode`` -> ``game_mode``,
  ``playerCount`` -> ``player_count``, ``gameVersion`` -> ``game_version``,
  ``gameId`` -> ``game_id``, ``workshopUrl`` -> ``workshop_url`` ...).

Envelope shapes actually returned by the live API (verified 2026-09-02), which
are *not* uniform:

* ``GET /mods/{id}``               -> ``{"status": "...", "mod": {...}}``
* ``GET /mods/{id}/versions``      -> ``{"status": ..., "data": {"versions": [...]}}``
* ``GET /mods/{id}/dependencies``  -> ``{"status": ..., "data": {"dependencies": [...]}}``
* ``GET /mods/{id}/scenarios``     -> ``{"status": ..., "data": {"scenarios": [...]}}``
* ``GET /mods?q=``                 -> ``{"status": ..., "meta": {...}, "data": [...]}``
* ``GET /rate-limits``             -> ``{"rate_limit": {...}}``  (no status wrapper)

A missing / deleted / blocked / private mod is a bare **HTTP 404** with no
reason code — raised here as :class:`ModNotFound`. The caller records that as
``api_state = not_found`` and never guesses which of the three it is.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger("reforger.mods.workshop")

BASE_URL = "https://api.reforgermods.net/v2"

# Rate-limit budget (from GET /v2/rate-limits, plan "free", scoped by client_ip).
_RATE_PER_MINUTE = 60
_RATE_BURST = 20
_RATE_PER_DAY = 20_000

_CACHE_TTL_SECONDS = 600.0  # 10 minutes
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
_MAX_429_RETRIES = 4
_MAX_TRANSIENT_RETRIES = 2
_SEARCH_PAGE_SIZE = 16  # the API's fixed page size for /mods (no limit/perPage param)


# --------------------------------------------------------------------- errors
class WorkshopError(RuntimeError):
    """Any failure talking to the Workshop API."""


class ModNotFound(WorkshopError):
    """HTTP 404 — the mod does not resolve (deleted, blocked or private;
    indistinguishable behind a bare 404)."""

    def __init__(self, mod_id: str) -> None:
        super().__init__(f"mod {mod_id!r} is not resolvable on the Workshop")
        self.mod_id = mod_id


class RateLimitExceeded(WorkshopError):
    """The daily budget is exhausted, or 429 retries were exceeded."""


# ------------------------------------------------------------ camelCase -> snake
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _camel_to_snake(key: str) -> str:
    return _CAMEL_BOUNDARY_RE.sub("_", key).lower()


def _convert(obj: Any) -> Any:
    """Recursively convert every mapping key from camelCase to snake_case."""
    if isinstance(obj, dict):
        return {_camel_to_snake(str(k)): _convert(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_convert(v) for v in obj]
    return obj


# ------------------------------------------------------------- rate limiter
class _RateLimiter:
    """Process-wide token bucket + daily counter.

    Capacity ``burst`` tokens, refilled at ``per_minute / 60`` tokens/second —
    which yields exactly the sustained rate while allowing a short burst. The
    lock is held across the pacing sleep so concurrent callers naturally queue
    and are released one refill-interval apart.
    """

    def __init__(
        self,
        per_minute: int = _RATE_PER_MINUTE,
        burst: int = _RATE_BURST,
        per_day: int = _RATE_PER_DAY,
    ) -> None:
        self._rate = per_minute / 60.0
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._per_day = per_day
        self._day = datetime.now(timezone.utc).date()
        self._day_count = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self._capacity, self._tokens + (now - self._updated) * self._rate
            )
            self._updated = now

            today = datetime.now(timezone.utc).date()
            if today != self._day:
                self._day = today
                self._day_count = 0
            if self._day_count >= self._per_day:
                raise RateLimitExceeded(
                    f"daily Workshop API budget ({self._per_day}) exhausted"
                )

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                logger.debug("workshop rate limiter: pacing %.2fs", wait)
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._updated = time.monotonic()
            else:
                self._tokens -= 1.0
            self._day_count += 1

    def note_retry_after(self, seconds: float) -> None:
        """After a 429: drain the bucket so the next acquire() also waits."""
        self._tokens = 0.0
        self._updated = time.monotonic() + max(0.0, seconds)


# Module singleton — shared by every WorkshopClient instance in the process.
_LIMITER = _RateLimiter()


# ------------------------------------------------------------------ TTL cache
class _TTLCache:
    def __init__(self, ttl: float = _CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._store.clear()


# --------------------------------------------------------------------- client
class WorkshopClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        ttl: float = _CACHE_TTL_SECONDS,
        timeout: httpx.Timeout | float = _HTTP_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._cache = _TTLCache(ttl)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        base_url=self._base_url,
                        timeout=self._timeout,
                        headers={"Accept": "application/json"},
                        follow_redirects=True,
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------- core GET
    async def _get_json(
        self, path: str, *, params: dict | None = None, use_cache: bool = True
    ) -> Any:
        request = (await self._http()).build_request("GET", path, params=params)
        cache_key = str(request.url)

        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        client = await self._http()
        attempt_429 = 0
        attempt_transient = 0
        while True:
            await _LIMITER.acquire()
            try:
                resp = await client.get(path, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                attempt_transient += 1
                if attempt_transient > _MAX_TRANSIENT_RETRIES:
                    raise WorkshopError(f"GET {path} failed: {exc}") from exc
                await asyncio.sleep(1.5 * attempt_transient)
                continue

            if resp.status_code == 404:
                raise ModNotFound(_mod_id_from_path(path))

            if resp.status_code == 429:
                attempt_429 += 1
                if attempt_429 > _MAX_429_RETRIES:
                    raise RateLimitExceeded(f"GET {path}: 429 after {attempt_429} retries")
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                logger.warning(
                    "workshop 429 on %s; backing off %.1fs (retry %d/%d)",
                    path, retry_after, attempt_429, _MAX_429_RETRIES,
                )
                _LIMITER.note_retry_after(retry_after)
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                attempt_transient += 1
                if attempt_transient > _MAX_TRANSIENT_RETRIES:
                    raise WorkshopError(f"GET {path}: HTTP {resp.status_code}")
                await asyncio.sleep(1.5 * attempt_transient)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise WorkshopError(f"GET {path}: HTTP {resp.status_code}") from exc

            try:
                payload = resp.json()
            except ValueError as exc:
                raise WorkshopError(f"GET {path}: response was not JSON") from exc

            data = _convert(payload)
            if use_cache:
                self._cache.set(cache_key, data)
            return data

    # ----------------------------------------------------------- public API
    async def get_mod(self, mod_id: str) -> dict:
        """The mod detail object (raises :class:`ModNotFound` on 404)."""
        payload = await self._get_json(f"/mods/{mod_id}")
        mod = payload.get("mod") if isinstance(payload, dict) else None
        if mod is None and isinstance(payload, dict):
            mod = payload.get("data")
        if not isinstance(mod, dict):
            raise WorkshopError(f"GET /mods/{mod_id}: no 'mod' object in response")
        return mod

    async def get_versions(self, mod_id: str) -> list[dict]:
        """``versions[]`` newest-first: ``version``, ``game_version``, ``size``,
        ``approved``, ``published``, ``created_at``, ``scenario_count``,
        ``dependency_count`` ..."""
        payload = await self._get_json(f"/mods/{mod_id}/versions")
        return _extract_list(payload, "versions")

    async def get_dependencies(self, mod_id: str) -> list[dict]:
        """``dependencies[]``: ``id``, ``name``, ``version``, ``size``,
        ``published``, ``private``, ``workshop_url`` ..."""
        payload = await self._get_json(f"/mods/{mod_id}/dependencies")
        return _extract_list(payload, "dependencies")

    async def get_scenarios(self, mod_id: str) -> list[dict]:
        """``scenarios[]``: ``name``, ``game_id``, ``game_mode``,
        ``player_count``, ``description``, ``author``."""
        payload = await self._get_json(f"/mods/{mod_id}/scenarios")
        return _extract_list(payload, "scenarios")

    async def search(self, q: str, limit: int = 20) -> list[dict]:
        """Search the Workshop by free text (``?q=``). The API paginates at a
        fixed 16/page with no size parameter, so pages are fetched until
        ``limit`` results are collected (capped at 5 pages)."""
        q = (q or "").strip()
        if not q:
            return []
        limit = max(1, min(int(limit), 5 * _SEARCH_PAGE_SIZE))
        out: list[dict] = []
        page = 1
        while len(out) < limit and page <= 5:
            payload = await self._get_json("/mods", params={"q": q, "page": page})
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                break
            out.extend(rows)
            meta = payload.get("meta") if isinstance(payload, dict) else None
            total_pages = (meta or {}).get("total_pages") if isinstance(meta, dict) else None
            if total_pages is not None and page >= int(total_pages):
                break
            page += 1
        return out[:limit]

    async def get_rate_limits(self) -> dict:
        """The raw ``/rate-limits`` body (no envelope). Best-effort/diagnostic."""
        payload = await self._get_json("/rate-limits", use_cache=False)
        return payload if isinstance(payload, dict) else {}


# ------------------------------------------------------------------- helpers
def _extract_list(payload: Any, key: str) -> list[dict]:
    """Pull ``key`` out of ``{"data": {key: [...]}}`` / ``{key: [...]}`` /
    ``{"data": [...]}`` / a bare list."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [x for x in data[key] if isinstance(x, dict)]
    if isinstance(payload.get(key), list):
        return [x for x in payload[key] if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _mod_id_from_path(path: str) -> str:
    m = re.search(r"/mods/([0-9A-Fa-f]{16})", path)
    return m.group(1) if m else path


def _parse_retry_after(value: str | None) -> float:
    if not value:
        return 5.0
    value = value.strip()
    try:
        return max(1.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delta = (when - datetime.now(timezone.utc)).total_seconds()
            return max(1.0, min(delta, 120.0))
    except (TypeError, ValueError):
        pass
    return 5.0


# Process-wide default client. API routes / the sync job share this so the TTL
# cache and (via _LIMITER) the rate budget are shared.
workshop = WorkshopClient()


# Thin module-level delegates (the public functional surface).
async def get_mod(mod_id: str) -> dict:
    return await workshop.get_mod(mod_id)


async def get_versions(mod_id: str) -> list[dict]:
    return await workshop.get_versions(mod_id)


async def get_dependencies(mod_id: str) -> list[dict]:
    return await workshop.get_dependencies(mod_id)


async def get_scenarios(mod_id: str) -> list[dict]:
    return await workshop.get_scenarios(mod_id)


async def search(q: str, limit: int = 20) -> list[dict]:
    return await workshop.search(q, limit)


def clear_cache() -> None:
    workshop.clear_cache()


async def aclose() -> None:
    await workshop.aclose()
