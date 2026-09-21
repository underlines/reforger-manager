# Sprint 13 — STORIES

## Shared reference (read before starting any story)

Line numbers may have drifted — **trust the symbol name**.

| Thing | Where | Notes |
|---|---|---|
| Detail route | `backend/app/api/mods.py:515` `get_mod_detail` | `get_versions` at :546, `resolve_dependencies` at :552 — both go out to the network |
| Dep resolver | `backend/app/mods/resolve.py:114` `resolve_dependencies` | already takes `use_api: bool = True`, `use_disk: bool = False` |
| DB dep read | `resolve.py:99` `_gproj_children` | reads `mod_dependencies` rows; what `use_api=False` falls through to |
| Graph route (DB-only model to copy) | `api/mods.py:286` `get_mods_graph` | no API, no BFS |
| Downloader | `backend/app/mods/downloader.py:372` `run_mod_download(ctx, guids, versions)` | |
| Config builder | `downloader.py:165` `_build_config` | omits `"version"` when unpinned ⇒ engine pulls **latest** |
| Engine runner | `downloader.py:244` `_run_engine` | 409s while a server runs; ready marker `downloader.py:61` |
| Post-download rescan | `backend/app/mods/sync.py:389` `refresh_local_mods(guids)` | disk rescan + upsert, no API |
| Update logic | `backend/app/mods/updates.py` | `_targets_for_scope` :149, `_pin` :217, `_apply_result` :263 |
| Free-space guard | `backend/app/mods/freespace.py:80` `guard_update_scope` | sums the **whole** scope — wrong for an idempotent refresh |
| `has_update` | `api/mods.py:80` `_has_update`; filter at :215; schema `schemas/mod.py:61` | |
| Enrichment | `sync.py:133` `enrich_one` (4 API calls); helpers `_upsert_api_scenarios` :252, `_upsert_api_dependencies` :296, `_ensure_dependency_stub_rows` :217 | |
| Sync job | `sync.py:412` `run_mod_sync` — pass 2 loop at :434 | |
| Scheduler | `backend/app/mods/schedule.py:112` `run_cycle`; ctor kwargs :63 | `check_updates_fn` is injected, tests stub it |
| Job registration | `backend/app/main.py:292` handlers, `:321` `job_manager.register` | |
| Package exports | `backend/app/mods/__init__.py:42` | |
| Server-scoped routes | `backend/app/api/servers.py:420` check / `:428` apply | |
| MCP specs | `backend/app/mcp/tools.py:778` check / `:792` apply | |
| FE detail | `frontend/src/pages/ModDetail.tsx` — type :32, badges :291, version card :329 | dep trees already come from `useModGraph`, **not** `dependency_tree` |
| FE library | `frontend/src/pages/Mods.tsx` — filter :411, buttons :503, badge :725 | |
| FE server panel | `frontend/src/components/server/ModsPanel.tsx:480` | |
| FE shared type | `frontend/src/lib/api.ts:50` `has_update` | |
| FE picker | `frontend/src/components/mods/ModLibraryPicker.tsx:184` | consumes `dependency_tree`; leave as-is |

**Test conventions.** Two styles exist; copy whichever the file you touch uses.
- pytest + `pytest_asyncio` fixture, route handler called directly with
  `session=session` — copy `backend/tests/test_mods_graph.py`.
- `unittest.IsolatedAsyncioTestCase` + `patch.object(module, "SessionLocal", ...)`
  — copy `backend/tests/test_updates.py`.

Both set `os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")`
and `sys.path.insert(0, str(Path(__file__).parents[1]))` **before** importing `app.*`.

Verify: `cd backend && .venv/Scripts/python.exe -m pytest -q`
(frontend also: `cd frontend && npm run build`).

## Parallelism

| Phase | Stories | Notes |
|---|---|---|
| 1 | S1, S2, S3 | independent files — run in parallel |
| 2 | S4 | edits `api/mods.py` (**after S1**), `api/servers.py`, `main.py`, `mods/__init__.py`; needs S2 + S3 landed |
| 2 | S5, S6 | independent of S4; S6 needs S2 |
| 3 | S7, S8, S9 | disjoint files — parallel; need S4 for the real API shape |
| 4 | S10 | orchestrator only |

Shared-file serialisation: **S1 → S4** (both `api/mods.py`).

---

## S1 — Detail route serves from the DB + on-demand versions route

**Deliver** (`backend/app/api/mods.py`)
- In `get_mod_detail`: delete the `versions` try/except block entirely (leave
  `versions=[]` in the `ModDetailOut(...)` construction); change the resolver
  call to `resolve_dependencies(session, [guid], use_api=False, use_disk=True)`.
- New route beside `/{guid}/dependencies`:
  ```python
  @router.get("/{guid}/versions", response_model=list[dict])
  async def get_mod_versions(guid: str) -> list[dict]:
      try:
          return await workshop.get_versions(guid.upper())
      except ModNotFound:
          return []
      except WorkshopError as exc:
          raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Workshop lookup failed: {exc}")
  ```
- Update the module docstring route list (`api/mods.py:3-8`).

**Test** — new `backend/tests/test_mod_detail_offline.py`, pytest style
(copy `test_mods_graph.py`), cover exactly:
1. `get_mod_detail` with a `Mod` + two `ModDependency` rows returns
   `versions == []` and a `dependency_tree` whose nodes/edges come from those
   rows — with `app.mods.resolve.workshop` patched to an object whose
   `get_dependencies` raises `AssertionError` (proves it is never called).
2. `get_mod_versions` returns the patched client's list.
3. `get_mod_versions` raises `HTTPException` 502 on `WorkshopError`.

**Watch out**
- `use_disk=True` touches the filesystem via `resolve_addon_dir`; with no addons
  root present it returns `None` and degrades cleanly — do not mock it away.
- Keep `versions` on `ModDetailOut` (`schemas/mod.py:116`); `ModLibraryPicker`
  still reads `dependency_tree`, so it must stay populated.
- Do not reorder routes: static segments before `/{guid}` (asserted by
  `test_phase3b_routes.py`).

---

## S2 — Update = idempotent engine downloader

**Deliver** (`backend/app/mods/updates.py`, full rewrite of the check/apply pair)
- Keep: `MOD_UPDATE_APPLY_JOB_KIND`, `UpdateScope`, `UpdateScopeError`,
  `normalize_scope`, `_pin`, `_MissingMod`.
- **Delete:** `MOD_UPDATE_CHECK_JOB_KIND`, `check_updates`,
  `make_check_updates_job`, `_check_updates`, `_record_workshop_state`,
  `_apply_result`, `_item_base`, and the `workshop` / `ApiState` imports.
- `_targets_for_scope` stays (it is pure DB) — strip nothing from it.
- New:
  ```python
  async def refresh_mods(scope: UpdateScope, ctx: "JobContext | None" = None) -> dict[str, Any]:
      """Re-download every mod in scope to its latest version (pins honoured).

      No Workshop API calls: the engine's own downloader is idempotent, so an
      addon already at the target version is a no-op.
      """
  ```
  - opens `SessionLocal()`, calls `_targets_for_scope(session, normalize_scope(scope))`;
  - `guids` = every target whose mod is not a `_MissingMod`;
  - `versions` = `{guid: pin["version"]}` for targets with a pin carrying a
    non-null `version` (the pin `_targets_for_scope` already resolved — server
    pin beats library pin);
  - `await run_mod_download(ctx, guids, versions)` then
    `await refresh_local_mods(guids)`;
  - returns `{"scope", "requested": guids, "pinned": versions, "downloaded": <run result>, "refreshed": <refresh_local_mods result>, "unavailable": [<missing guids>]}`;
  - empty `guids` → return the same shape with `downloaded={"guids": [], "progress": 100.0}` and **no** downloader call.
- `make_apply_updates_job(scope)` returns a closure calling `refresh_mods(scope, ctx)`.
- Import `refresh_local_mods` from `.sync`.

**Test** — rewrite `backend/tests/test_updates.py` (keep the fixture shape and
`test_scope_validation`), cover exactly:
1. `"all"` scope → downloader awaited with every local GUID; `versions` dict
   holds only the library-pinned mod.
2. server scope → the server pin wins over the library pin in `versions`.
3. `refresh_local_mods` awaited with the same GUID list.
4. no workshop calls — patch `updates.workshop` out of existence by asserting
   the module has no such attribute after the rewrite (`hasattr` check).
5. empty scope (no local mods) → downloader not awaited.

**Watch out**
- `run_mod_download` validates GUIDs with `_normalise_inputs` and **raises** if
  a version pin names an unrequested GUID — build `versions` only from `guids`.
- Pins may be `{"source": ..., "version": None}` — skip those.
- `refresh_local_mods` opens its own session; do not pass it one.

---

## S3 — Free-space guard fit for an idempotent refresh

**Deliver** (`backend/app/mods/freespace.py`)
- Add:
  ```python
  async def guard_refresh_scope(session: AsyncSession, scope: str | int) -> None:
      """Free-space guard for an idempotent refresh.

      The engine stages one addon in a temp dir before moving it, and a refresh
      re-downloads only what actually changed — so the peak requirement is the
      largest single addon, not the sum of the scope.
      """
  ```
  - same scope query as `guard_update_scope` (`is_local` for `"all"`; enabled
    `ServerMod` join for a server id);
  - `known = [m.size for m in mods if m.size]`; if empty → `logger.info(...)` and return;
  - `projected = max(known) * 2`; `check_free_space(settings.mods_dir, projected)`.
- **Delete** `guard_update_scope` and `ensure_sizes`' use by it — `ensure_sizes`
  itself stays (still used by the per-mod download routes).

**Test** — new `backend/tests/test_freespace_refresh.py`, pytest style,
cover exactly:
1. allows when free space exceeds `max × 2` (patch `shutil.disk_usage`).
2. raises `HTTPException` 409 when it does not.
3. mods with `size=None` are skipped, the known max still drives the projection.
4. all-unknown / empty scope returns without raising.

**Watch out**
- `check_free_space` treats `projected is None` as "refuse" — never pass `None`
  from this function; return early instead.
- Patch `shutil.disk_usage` where `freespace` imported it (module attribute),
  and return an object with a `.free` attribute.

---

## S4 — Wire the routes, delete the check job kind

**Depends on S1 (same file) + S2 + S3.**

**Deliver**
- `backend/app/api/mods.py`: delete `check_all_updates` and the
  `/updates/check` route; `apply_all_updates` now calls
  `guard_refresh_scope(session, "all")`. Fix the imports
  (`MOD_UPDATE_CHECK_JOB_KIND` and `guard_update_scope` are gone).
- `backend/app/api/servers.py`: delete the `/mods/update/check` route (`:420`);
  the apply route (`:428`) uses `guard_refresh_scope(session, server_id)`.
  Check whether it currently guards at all — if not, add it.
- `backend/app/main.py`: delete `_job_update_check` (`:292`), its
  `job_manager.register` line (`:321`), and the now-dead imports.
- `backend/app/mods/__init__.py`: drop `check_updates`,
  `make_check_updates_job`, `MOD_UPDATE_CHECK_JOB_KIND` from the imports and
  `__all__`.
- Remove `_has_update` and the `?update=` query filter from `list_mods`; drop
  `has_update` from `backend/app/schemas/mod.py` (`ModOut`). Leave
  `_to_out`'s other assignments alone.

**Test** — edit `backend/tests/test_phase3b_routes.py`: remove the two
`/mods/updates/check` assertions (`:18-19`), keep the `/updates/apply` ordering
assertion. Run the whole suite and fix any other red caused by the removals —
**no new test files in this story.**

**Watch out**
- `test_storage_and_download.py:146` posts to `/api/mods/updates/apply` and
  `:150` asserts the server apply guard fires — both must still pass; the
  expected 409 message changes if the projection changes, so update the
  assertion text rather than the guard.
- `latest_version` / `latest_game_version` columns stay. Only `has_update` goes.

---

## S5 — Trim `mod_sync` enrichment to scenarios + dependencies

**Deliver** (`backend/app/mods/sync.py`)
- New `enrich_local_one(session, guid, *, client=None) -> Mod`: same shape as
  `enrich_one` but calls only `client.get_scenarios(guid)` and
  `client.get_dependencies(guid)`. On `ModNotFound` → `api_state = not_found`,
  `api_checked_at = now`, flush, return. On success → `api_state = ok`,
  `api_checked_at = last_checked = now`, then `_upsert_api_scenarios`,
  `_upsert_api_dependencies`, `_ensure_dependency_stub_rows`, flush.
- `run_mod_sync` pass 2 calls `enrich_local_one` instead of `enrich_one`.
- Update the module docstring (`sync.py:14-19`) to say pass 2 is scenarios +
  dependencies only, and why (`latest_*` is no longer used by update logic).
- **`enrich_one` is unchanged** — still used by `POST /api/mods/add` and
  `freespace.ensure_sizes`.

**Test** — no new file. Run `test_mod_sync_scenario_preservation.py`,
`test_sync_scenario_repair.py`, `test_enrich_size.py`,
`test_metadata_selfheal.py` and fix fallout only.

**Watch out**
- Do not touch `latest_version` in `enrich_local_one` — sync no longer maintains it.
- `_ensure_dependency_stub_rows` must still run, or dependency-only addons
  vanish from the pickers.
- Fake clients in the existing tests may not define `get_scenarios` /
  `get_dependencies`; extend the fakes, not the production code.

---

## S6 — Rewire the nightly scheduler (still disabled)

**Depends on S2.**

**Deliver** (`backend/app/mods/schedule.py`)
- Replace the `check_updates_fn` ctor kwarg with
  `refresh_mods_fn: Callable[[str], Awaitable[dict[str, Any]]] = refresh_mods`
  (import from `.updates`); store as `self._refresh_mods`.
- `run_cycle`: `updates = await self._refresh_mods("all")` — keep it in the
  same position (after `refresh_engine`, before `_mod_sync`) and keep the
  summary key `"updates"`.
- Class docstring: note it is disabled by default and that enabling it means
  unattended multi-GB downloads.
- **Do not** change `nightly_check_enabled` in `backend/app/core/config.py`.

**Test** — edit `backend/tests/test_schedule.py`: rename the injected stub to
`refresh_mods_fn` (`:50`, `:81`) and assert `run_cycle` awaited it with `"all"`.
No new cases.

**Watch out**
- `_ScheduleContext` is not a real `JobContext`; `refresh_mods("all")` is called
  with no ctx — make sure S2's signature defaults `ctx=None`.

---

## S7 — Frontend: mod detail page

**Deliver** (`frontend/src/pages/ModDetail.tsx`)
- Drop `has_update` from the `ModDetail` type and the badge at `:291`.
- Staleness line under the Summary metadata row, from
  `detail.api_checked_at ?? detail.last_checked`:
  - null → amber "Never checked against the Workshop";
  - < 30 days → stone-400 "Checked {relative} ago";
  - ≥ 30 days → amber "Checked {relative} ago — may be out of date".
  Write a small local `relativeAge(iso: string): string` helper (days / months);
  no new dependency.
- Version History card: `useQuery` with `enabled: false` (or a `useState`
  trigger) against `/api/mods/${guid}/versions`; render a
  `<Button size="sm">Load version history</Button>` until loaded, then the
  existing table. Keep the existing empty-state copy for a `[]` result.
- Pin dialog default (`:201`) → `mod?.installed_version ?? ""`.

**Done when** `npm run build` passes.

**Watch out**
- `detail.versions` is now always `[]` from the detail payload — render from
  the new query's data, not from `detail.versions`.
- Leave `useModGraph` and the two `ModTree` blocks alone; they are already
  DB-backed.

---

## S8 — Frontend: library page + server mods panel

**Deliver**
- `frontend/src/pages/Mods.tsx`: remove the "Check updates" button (`:503`);
  relabel "Apply all updates" → **"Update all mods"** (`:506-512`); remove the
  `updatesOnly` filter state, its control, the `:411` predicate line, and the
  `Update` badge (`:725`); drop `has_update` from the local `ModRecord` type
  (`:37`).
- `frontend/src/components/server/ModsPanel.tsx`: remove the "Check updates"
  button (`:480-491`); relabel "Apply updates" → **"Update mods"**.
- `frontend/src/lib/api.ts:50`: remove `has_update`.

**Done when** `npm run build` passes and no `has_update` reference remains
(`rg has_update frontend/src` is empty).

**Watch out**
- `updatesOnly` is referenced in a `useMemo` dependency array — remove it there
  too or the build fails.

---

## S9 — MCP tool surface

**Deliver** (`backend/app/mcp/tools.py`)
- Delete the `check_all_mod_updates` spec (`:778`) and the server-scoped
  `check_server_mod_updates` spec.
- Rewrite `apply_all_mod_updates` (`:792`) and `apply_server_mod_updates`
  descriptions: the engine's own downloader refreshes every mod in scope to its
  latest version, pins honoured, transferring only what actually changed; no
  Workshop API is consulted; 409 while a server runs or the free-space guard
  trips; returns `{job_id, kind}` to poll with `wait_for_job`.

**Test** — no new file. Run `test_mcp_tools_read.py`,
`test_mcp_tools_mutations.py`, `test_mcp_end_to_end.py`, `test_mcp_composites.py`
and fix any spec-count / name assertions.

**Watch out**
- Some MCP tests assert an exact tool-name set — update those lists.

---

## S10 — Live verification (orchestrator)

Per PLAN "Verification". Rebuild the stack **after** S7/S8 land (the SPA is
baked into the image), drive the detail page via chrome-devtools MCP, and
confirm no `api.reforgermods.net` request is made on a cold detail view
(`docker compose logs reforger-manager`). Write `.sprints/13/RESULTS.md`.

**Production TrueNAS is out of scope.**
