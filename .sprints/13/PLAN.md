# Sprint 13 — PLAN

**Date:** 2026-09-21
**Title:** Mod detail from the DB; drop the 3rd-party API from update detection

## Context

`GET /api/mods/{guid}` was observed taking ~2 min, then loading instantly for
~10 min. Cause is not the dependency BFS (both reported mods have a single
engine-builtin dependency) but the Workshop client:

- one process-wide token bucket, 60 req/min, shared by interactive page loads
  and bulk jobs (`workshop.py:100`) — a page load queues behind a 259-mod job;
- a 10-min in-memory per-URL TTL cache (`workshop.py:57`) — hence "fast the
  second time".

The detail route makes 2+ live calls per view for data the DB already holds.

## Guiding principle

The DB is the source of truth for anything about a mod we already have on disk.
The 3rd-party API is touched only when the user asks for something the local
data genuinely cannot answer. Updating mods is the engine's job, not the API's.

## Decisions locked

| # | Decision |
|---|---|
| 1 | Drop `has_update` / update-available polling. "Update all mods" = idempotent headless engine downloader; it transfers only what changed. |
| 2 | Version History card stays, **load on demand** via a new route. Not in the detail payload. |
| 3 | Workshop API retained for: **search / add a mod**, and **scenario lists**. Nothing else. |
| 4 | Nightly scheduler stays **disabled**; its mod step is rewired to the downloader so enabling it later is correct. |
| 5 | Detail page shows "checked <age>"; stronger hint past **30 days**. |
| 6 | No schema change. `latest_version` / `latest_game_version` columns stay, unused by update logic. |

## Code-level facts the implementation must respect

1. `get_mod_detail` — `backend/app/api/mods.py:515`. Two live calls:
   `workshop.get_versions(guid)` (**:546**) and `resolve_dependencies(session, [guid])`
   (**:552**, BFS calling `client.get_dependencies` per node).
2. `resolve_dependencies` **already supports** `use_api=False, use_disk=True`
   (`backend/app/mods/resolve.py:114`). With `use_api=False` it reads
   `mod_dependencies` rows via `_gproj_children` (`resolve.py:99`), then on-disk
   `addon.gproj`. DB-only is a keyword-argument change, not a rewrite.
3. `_build_config` — `backend/app/mods/downloader.py:165`. Omits `"version"`
   when the GUID has no pin, so the engine fetches **latest**. Passing every
   local GUID with no version pins **is** the idempotent update-all primitive;
   already-current addons are a no-op. This is the whole of feature 2.
4. `_run_engine` — `downloader.py:244`. Refuses while a server runs (`:245`),
   stops at `_READY_MARKER` "Required addons are ready to use." (`:61`).
   `_FATAL_RE` (`:48`) aborts on blocked/missing addons.
5. `refresh_local_mods(guids)` — `backend/app/mods/sync.py:389`. Re-scans disk
   and upserts rows, **no API**. Must run after the downloader so
   `installed_version` reflects what actually landed.
6. `guard_update_scope` — `backend/app/mods/freespace.py:80`. For `"all"` it
   **sums every local mod's size** (tens of GB) and 409s when that does not fit.
   Against an idempotent refresh that usually transfers ~nothing, that guard is
   wrong and would refuse almost every run. It needs a peak-based projection.
7. `_has_update` — `api/mods.py:80`; the `?update=` list filter uses the same
   two columns (`api/mods.py:215`).
8. The frontend **already** renders both dependency trees from
   `/api/mods/graph` (DB-only, no API — `api/mods.py:286`) via `useModGraph`.
   `detail.dependency_tree` is **unused** in `ModDetail.tsx`; its only consumer
   is `ModLibraryPicker.tsx:184`, which fetches `/api/mods/{guid}` per opened
   disclosure — a second hot path fixed by fact 2.
9. `enrich_one` — `sync.py:133`. Four API calls per mod (`get_mod`,
   `get_versions`, `get_scenarios`, `get_dependencies`). `mod_sync` pass 2 runs
   it for every scanned mod (~259 × 4). It is also the `POST /api/mods/add` and
   `ensure_sizes` path — **those keep the full version**.
10. Dropping `get_dependencies` from sync would risk the config-closure
    regression (`mod_dependencies` feeds `config_gen`). **Keep it.** Only
    `get_mod` + `get_versions` come out of the sync path.
11. `NightlyCheckScheduler.run_cycle` — `backend/app/mods/schedule.py:112`,
    calls `self._check_updates("all")` then `self._mod_sync(ctx)`. Injectables
    are constructor kwargs (`:63`), which is how the tests stub them.
    `nightly_check_enabled` default `False` — `backend/app/core/config.py:68`.
12. Job kinds are registered in `backend/app/main.py:321`; removing a kind means
    removing its registration and its `mods/__init__.py` re-export (`:42`).
13. MCP tool specs describing the old semantics: `backend/app/mcp/tools.py:778`
    (`check_all_mod_updates`) and `:792` (`apply_all_mod_updates`); the
    server-scoped pair lives alongside the server routes.
14. Route order matters — static paths must precede `/{guid}`
    (`api/mods.py:279`, asserted by `tests/test_phase3b_routes.py:18`).

## Feature set

### F1 — Detail route serves from the DB
- `api/mods.py:get_mod_detail`: delete the `get_versions` try/except; call
  `resolve_dependencies(session, [guid], use_api=False, use_disk=True)`.
- `versions` stays on `ModDetailOut` but is always `[]` here.
- Result: zero Workshop calls on the hot path.

### F2 — On-demand version history
- New `GET /api/mods/{guid}/versions` → `list[dict]`, live `workshop.get_versions`.
  404 → `[]`; `WorkshopError` → 502. Declared before `/{guid}` is not required
  (it is a sub-path) but keep it beside `/{guid}/dependencies`.

### F3 — Update = idempotent engine downloader
- `mods/updates.py`: replace the check/apply pair with `make_refresh_job(scope)`.
  - targets: `scope == "all"` → every `is_local` mod; server id → enabled
    assigned mods (reuse `_targets_for_scope`, minus the API loop).
  - `versions` dict carries **only pinned** targets (server pin beats library
    pin for a server scope) so a pin is honoured and everything else goes latest.
  - `run_mod_download(ctx, guids, versions)` → then `refresh_local_mods(guids)`.
  - returns `{scope, requested, pinned, downloaded, refreshed}`. No API calls.
- Job kind `mod_update_apply` is reused (path/MCP compatibility).
- **Removed:** `check_updates`, `make_check_updates_job`,
  `MOD_UPDATE_CHECK_JOB_KIND`, `POST /api/mods/updates/check`,
  `POST /api/servers/{id}/mods/update/check`, and their registrations/exports.

### F4 — Free-space guard fit for an idempotent refresh
- `freespace.py`: add `guard_refresh_scope(session, scope)` used by the apply
  routes. Projection = `max(known size in scope) * 2` (the engine stages one
  addon in a temp dir before moving it), **not** the sum. Unknown sizes are
  skipped, not fatal; if no size is known at all, log and allow.
- `guard_update_scope` is deleted with its only callers.

### F5 — Drop the update-available concept
- Remove `_has_update`, `ModOut.has_update`, the `?update=` filter.
- Frontend: remove the `Update` / `Update available` badges and the
  "updates only" filter.
- Columns stay in the DB; nothing writes them from update logic any more.

### F6 — Trim `mod_sync` enrichment
- New `enrich_local_one(session, guid, *, client)` in `sync.py`: `get_scenarios`
  + `get_dependencies` only, reusing `_upsert_api_scenarios` /
  `_upsert_api_dependencies` / `_ensure_dependency_stub_rows`, setting
  `api_state` / `api_checked_at` / `last_checked` the same way.
- `run_mod_sync` pass 2 calls it instead of `enrich_one`. Halves sync API load
  and keeps the dependency closure intact.
- `enrich_one` untouched — still `POST /api/mods/add` and `ensure_sizes`.

### F7 — Scheduler rewired, still off
- `NightlyCheckScheduler`: replace the `check_updates_fn` kwarg with
  `refresh_mods_fn` defaulting to the F3 refresh; `run_cycle` calls it.
- `nightly_check_enabled` stays `False`. Docstring says enabling it downloads.

### F8 — Frontend detail page
- Staleness line from `api_checked_at ?? last_checked`: "Checked 3 days ago";
  past **30 days** or null → amber "Checked 2 months ago — may be out of date".
- Version History card: button → `GET /api/mods/{guid}/versions`, renders on
  success. No fetch on mount.
- Drop `has_update` from the type and the badge row. Pin dialog defaults to
  `installed_version`.

### F9 — Frontend library page + MCP
- `Mods.tsx`: remove "Check updates"; "Apply all updates" → "Update all mods";
  drop the updates-only filter and the `Update` badge.
- `ModsPanel.tsx`: remove the server "Check updates" button; "Apply updates"
  → "Update mods".
- `mcp/tools.py`: delete the two check specs; rewrite the two apply
  descriptions to say the engine downloader refreshes every mod in scope to
  latest (pins honoured) and transfers only what changed.

## Data-model changes

**None.** No new columns, no migration.

## Testing

| File | Cases |
|---|---|
| `test_mod_detail_offline.py` (new) | detail route makes **zero** workshop calls; `versions == []`; `dependency_tree` built from `mod_dependencies` rows; `GET /{guid}/versions` returns live data and 502s on `WorkshopError`. |
| `test_updates.py` (rewrite) | refresh passes every local GUID; pinned-only versions dict; server scope prefers the server pin; `refresh_local_mods` called with the same GUIDs; no workshop attribute is touched. |
| `test_freespace_refresh.py` (new) | `guard_refresh_scope` allows when free > max×2; 409s when not; unknown sizes skipped; empty/all-unknown scope allowed. |
| `test_schedule.py` (edit) | `run_cycle` calls the refresh fn, not a check fn. |
| `test_phase3b_routes.py` (edit) | the two `/updates/check` assertions removed; `/updates/apply` order assertion kept. |
| `test_mod_sync_scenario_preservation.py` (edit if red) | keep green under `enrich_local_one`. |

Proportionate only — no new coverage beyond the above.

## Cut from this sprint / non-goals

- No priority lane or per-caller quota in the rate limiter — removing the hot
  path from the API makes it moot.
- No persisted version history table.
- Nightly scheduler is **not** enabled.
- `latest_version` columns are not dropped (no migration risk).
- Production TrueNAS untouched.

## Verification

```bash
cd backend && .venv/Scripts/python.exe -m pytest -q
cd frontend && npm run build
docker compose up -d --build
```

Manual (local dev, chrome-devtools MCP, `localhost:18090`):
1. Open a mod detail page cold — it renders without a Workshop round trip;
   confirm in the container log that no `api.reforgermods.net` request is made.
2. Click "Load version history" — the table fills.
3. Confirm the staleness label, and the amber variant on a mod whose
   `api_checked_at` is older than 30 days.
4. "Update all mods" enqueues a `mod_update_apply` job that launches the
   headless downloader (server stopped).
