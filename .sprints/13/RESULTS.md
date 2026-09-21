# Reforger Manager — Sprint 13 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-21 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: complete

All 9 code stories (S1–S9) implemented; S9's deliverable was absorbed into S4
(see below). Backend suite green (401 passed), frontend build green, and the
core claim of the sprint — the mod detail route makes zero Workshop API calls
— verified live against a rebuilt Docker stack.

## Story-by-story

| # | Story | Agent | State | Notes |
|---|---|---|---|---|
| S1 | Detail route serves from the DB + on-demand versions route | Claude | done | `get_mod_detail` now resolves dependencies via `resolve_dependencies(session, [guid], use_api=False, use_disk=True)` and always returns `versions=[]`. New `GET /{guid}/versions` route added beside `/{guid}/dependencies`, live Workshop call only on request. New `test_mod_detail_offline.py` (3 cases). One bug found post-merge: the test compared dependency-tree edges as objects (`e.from_`/`e.to`) when the route actually returns them as `{"from", "to"}` dicts — fixed directly by the orchestrator. |
| S2 | Update = idempotent engine downloader | Claude | done | Rewrote `mods/updates.py`: deleted the whole check/apply-with-Workshop-state pair (`check_updates`, `make_check_updates_job`, `MOD_UPDATE_CHECK_JOB_KIND`, `_record_workshop_state`, `_apply_result`, `_item_base`). New `refresh_mods(scope, ctx=None)` re-runs the engine's downloader over every local/scoped GUID (server pin beats library pin), then `refresh_local_mods`. `make_apply_updates_job(scope)` wraps it. Rewrote `test_updates.py`, 6 cases (5 required + `test_scope_validation` kept), confirms `updates` module no longer imports `workshop` at all. |
| S3 | Free-space guard fit for an idempotent refresh | opencode·deepseek | done | New `guard_refresh_scope(session, scope)` in `freespace.py`: projection = `max(known mod sizes) * 2` (engine stages one addon at a time), unknown/empty sizes are skipped, not fatal. `guard_update_scope` (sum-based, wrong for a no-op-mostly refresh) deleted. New `test_freespace_refresh.py`, 4 cases. Stayed exactly in scope (only `freespace.py` + its test touched). |
| S4 | Wire the routes, delete the check job kind | Claude | done | Fixed every call site left broken by S1–S3: `mods/__init__.py` exports, `api/mods.py` (deleted `/updates/check`, `apply_all_updates` now uses `guard_refresh_scope`, removed `_has_update` + the `?update=` filter), `api/servers.py` (deleted `/mods/update/check`, apply route now guarded with `guard_refresh_scope`), `main.py` (deleted the check job registration), `schemas/mod.py` (dropped `ModOut.has_update`). Also fixed fallout in `test_phase3b_routes.py`, `test_mcp_tools_mutations.py`, `test_mcp_tools_read.py`, `test_metadata_selfheal.py`, `test_mod_dependents.py` — and, since the full suite wouldn't pass otherwise, did S9's MCP-spec rewrite in `mcp/tools.py` too (see S9 below). Full suite green at hand-off except the S1 test bug noted above, which the orchestrator fixed directly. |
| S5 | Trim `mod_sync` enrichment to scenarios + dependencies | opencode·glm | done | New `enrich_local_one` in `sync.py`: only `get_scenarios` + `get_dependencies` (no `get_mod`/`get_versions`), reusing the existing upsert/stub helpers. `run_mod_sync` pass 2 now calls it instead of `enrich_one`, halving sync API load. `enrich_one` itself untouched — still backs `POST /api/mods/add` and `ensure_sizes`. No new test file; existing sync tests stayed green as-is. |
| S6 | Rewire the nightly scheduler (still disabled) | opencode·deepseek | done | `NightlyCheckScheduler` now takes `refresh_mods_fn` (defaulting to `updates.refresh_mods`) instead of `check_updates_fn`; `run_cycle` calls it with `"all"` in the same position as before. `nightly_check_enabled` untouched (`False`). `test_schedule.py` updated for the renamed stub. |
| S7 | Frontend: mod detail page | opencode·glm | done | Dropped `has_update` from the local type + badge row. Added a staleness line (`api_checked_at ?? last_checked`; amber past 30 days or when null) with a local `relativeAge()` helper. Version History card now uses a disabled-by-default `useQuery` (`enabled: false`, no refetch-on-mount/focus/reconnect) behind a "Load version history" button, rendering from the query's own data instead of `detail.versions` (which is now always `[]`). Pin dialog defaults to `installed_version`. |
| S8 | Frontend: library page + server mods panel | Claude | done | `Mods.tsx`: removed "Check updates", relabelled "Apply all updates" → "Update all mods", removed the `updatesOnly` filter state/UI/`useMemo` dependency, removed the `Update` badge, dropped `has_update` from the local type. `ModsPanel.tsx`: removed "Check updates", relabelled "Apply updates" → "Update mods". `lib/api.ts`: dropped `has_update` from `Mod`. `rg has_update frontend/src` clean after S7 landed too. |
| S9 | MCP tool surface | — (done by S4) | done | Deleting `check_all_mod_updates` / `check_server_mod_updates` and rewording the two `apply_*_mod_updates` descriptions was required for `mcp/tools.py` to still import after S2's rewrite, so S4 did it directly rather than leaving the tree broken for a separate hand-off. Verified the result matches S9's spec exactly (no check specs remain; both apply descriptions describe the idempotent-refresh, pins-honoured, no-Workshop-call behaviour). No separate story run. |

## Data-model changes

**None**, as planned. `latest_version` / `latest_game_version` columns remain
in the schema, unused by update logic (nothing writes them from the removed
check path any more).

## New / changed API surface

- New: `GET /api/mods/{guid}/versions` (live Workshop lookup, on demand).
- Removed: `POST /api/mods/updates/check`, `POST /api/servers/{id}/mods/update/check`.
- Changed: `POST /api/mods/updates/apply` and `POST /api/servers/{id}/mods/update/apply`
  now trigger `refresh_mods` (idempotent engine re-download honouring pins) instead
  of "apply a detected update"; both guarded by `guard_refresh_scope` (peak-based,
  not sum-based).
- Removed: `ModOut.has_update` and the `list_mods` `?update=` filter.
- MCP: `check_all_mod_updates` / `check_server_mod_updates` tool specs removed;
  `apply_all_mod_updates` / `apply_server_mod_updates` descriptions rewritten.

## Verification performed

- **Backend:** `cd backend && ./.venv/Scripts/python.exe -m pytest -q` →
  **401 passed**, 0 failed (baseline before the sprint: 397; net +4 across the
  two new test files minus consolidations).
- **Frontend:** `npm run build` (`tsc -b && vite build`) green. `rg has_update
  frontend/src` returns no matches.
- **Docker:** `docker compose up -d --build` — rebuilt clean, both containers
  healthy.
- **Live, via chrome-devtools MCP against `localhost:18090`** (logged in as
  `admin`):
  1. Opened Mod Library — confirmed "UPDATE ALL MODS" (renamed), no "Check
     updates" button, no per-row "Update"/"Update available" badge.
  2. Opened a mod detail page cold (`/mods/61B514B96692C049`) — network log
     shows exactly one relevant request, `GET /api/mods/61B514B96692C049`;
     **no** request to `api.reforgermods.net`. Container log for the same
     window shows only the local route hit, no outbound Workshop call.
     Staleness line rendered correctly ("Checked 2 days ago").
  3. Clicked "Load version history" — fired `GET
     /api/mods/61B514B96692C049/versions`, and the container log shows exactly
     one `HTTP Request: GET https://api.reforgermods.net/v2/mods/.../versions`
     at that moment (and only then) — confirming the Workshop call is fully
     on-demand. The table populated with real version/game-version rows.

## Not exercised

- The 30-day-old "may be out of date" amber variant of the staleness label —
  no mod in the local dev library had `api_checked_at`/`last_checked` that old
  at verification time. Logic was code-reviewed instead (see S7).
- Actually running "Update all mods" / a server-scoped "Update mods" to
  completion (would download real Workshop content into the dev addon cache —
  out of proportion for this sprint's verification).
- The nightly scheduler end-to-end (it stays disabled by design — S6 is wiring
  only).
- `guard_refresh_scope`'s 409 path against a real full disk (unit-tested only).
- Production TrueNAS — untouched, per PLAN "Cut from this sprint".

## How this sprint was implemented

Split per the user's request: roughly half the stories on native Sonnet
subagents, half across the two opencode models. Claude (Sonnet) took the
foundational/shared-file stories — S1, S2, S4 (backend wiring) and S8
(frontend library page) — while opencode·deepseek took S3 (freespace guard)
and S6 (scheduler rewire), and opencode·glm took S5 (sync trim) and S7
(frontend detail page). Connectivity to both opencode models was checked
before dispatch; both produced real diffs in-scope on their first attempt, no
restarts needed. Stories ran in the dependency-ordered phases from
STORIES.md — Phase 1 (S1/S2/S3 parallel) → Phase 2 (S4 after all three landed,
S5/S6 in parallel alongside it) → Phase 3 (S7/S8 in parallel, once S4's API
shape was live) — with the orchestrator running the full backend suite after
each phase and fixing the one cross-story test bug (S1's edge-shape assertion)
directly rather than re-dispatching it. S9's deliverable ended up folded into
S4 because the full suite could not pass without it; verified separately
against S9's own acceptance criteria before being marked done.
