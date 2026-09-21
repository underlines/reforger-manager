# Reforger Manager — Sprint 14 implementation stories

> Context, decisions, code facts and verification live in [PLAN.md](PLAN.md). `§n` = PLAN feature
> numbers, `fact #n` = *Code-level facts* section.

## Shared reference (read before starting any story)

| Thing | Where | Notes |
|---|---|---|
| Soft-delete guard | `backend/app/api/mods.py:398-439` `delete_local_mod` | fact #1 |
| Full-delete guard (unchanged) | `backend/app/api/mods.py:442-479` `delete_library_mod` | |
| Reference/closure sets | `backend/app/api/storage.py:50-72` `orphan_reference_sets` | returns `(directly_referenced, closure_owners)` |
| Block-detail message | `backend/app/api/mods.py:159-185` `_delete_block_detail` | reused — but the soft-delete path needs dependency-only wording after S1 (fact #1) |
| Resolved server mod list | `backend/app/servers/config_gen.py:49-61` `ModEntry`, `:162-259` `resolved_mod_entries` | fact #3 — diff against this, not a re-resolve. `ModEntry(mod_id: str, name: str \| None, version: str \| None)`; `version` is `None` when unpinned. Needs a server loaded via `_load` (`selectinload(Server.mods)`) or it raises `MissingGreenlet` |
| Dependency resolver | `backend/app/mods/resolve.py:114-239` `resolve_dependencies` | fact #2 |
| Preflight | `backend/app/servers/preflight.py` `preflight()` | `resolved_mods` entries built ~line 111-127 |
| Download job internals | `backend/app/mods/downloader.py` | `_enqueue_mod_download` lives in `mods.py:356-375` (extract its **tail only** — the 404 loop at :367 stays put, fact #5); `ensure_mods_ready` (:428-457) is NOT reused (locked decision) |
| Job reconciliation | `backend/app/main.py:239-276` `_job_mod_download` | fact #4 — reads `params["guids"]`/`["versions"]`, flips `is_local` via `refresh_local_mods` |
| Job engine | `backend/app/core/jobs.py` `JobManager` | single `asyncio.Queue` (:68), single worker task (:73, :88), serial `_run` loop at **:192-202**; class decl→`enqueue` is 65-163 |
| Route precedent | `backend/app/api/servers.py:417-422` `apply_server_updates` | shape to copy: `_load` → guard → `job_manager.enqueue` → job schema. `_load` is `servers.py:82-90` |
| Job schemas | `backend/app/schemas/job.py` | `JobEnqueuedOut{job_id:int, kind:str}` — new schema must allow `job_id: int | None` |
| Danger styling | `frontend/src/components/ui.tsx:64-82` `Badge` | `tone="bad"` = the only existing warning precedent |
| Delete dialogs (library list) | `frontend/src/pages/Mods.tsx` | mutations 304-334, `refTarget` + references query 245-250, `renderReferences` **446-486**, dialogs 893-925 (disk) / 926-962 (library), inline `deleteError` at 905 / 942 |
| Delete dialog (detail page) | `frontend/src/pages/ModDetail.tsx` | `deleteMutation` 205-217 (closes dialog on error — bug, fact #11), dialog 513-554, `required_by` block 528-534, "Used By" card 479-510 (`detail.used_by`, servers only), action cluster **268-296** (already 5 buttons incl. "Remove from library" 292-294), no disk-only action today |
| References endpoint | `GET /api/mods/{guid}/references` → `backend/app/api/mods.py:482-488` | returns servers + modpacks + required_by; already what `Mods.tsx` queries live, what `ModDetail.tsx` should start querying live too instead of relying on `ModDetailOut.used_by`/`required_by` |
| Job polling precedent | `frontend/src/lib/modGraph.ts:35-51` `useInvalidateGraphOnJob` | fact #8 |
| `/start` call sites | `ServerRow.tsx:21-38` (line 32), `SavesPanel.tsx:276`, `SavesPanel.tsx:334` | fact #9 — grep-confirmed the only three |
| Busy-state wrapper | `ServerDetail.tsx:36-48` `run(name, request)` | **not** defined in `SavesPanel.tsx` (prop, typed :144); `setAction(name)` captures the label once — static label only, don't change the signature |
| Shared types | `frontend/src/lib/api.ts` | `Job` (66-74, has `error: string \| null` at 72), `Preflight` (109-119 — the only typed surface for `resolved_mods`; the backend returns a raw `dict`, no `response_model`), `api`/`apiVoid`/`ApiError` (~230-270) |
| Test precedent (guard) | `backend/tests/test_storage_and_download.py:197-244` | `test_delete_local_409_when_directly_referenced` (224) must flip to 200; `test_delete_local_409_for_kept_dependency` (233) must stay 409 unchanged — use as regression guard |
| Test precedent (download route) | `backend/tests/test_storage_and_download.py:104-141` | free-space-guard / job-enqueue assertions to mirror for the new route |
| Test precedent (preflight) | `backend/tests/test_preflight.py`, `test_preflight_blocked.py` | |

Windows venv python: `backend/.venv/Scripts/python.exe -m pytest -q`. Frontend gate: `npm run build` only — no new frontend tests.

## Parallelism

| Phase | Stories | Notes |
|---|---|---|
| 0 | S1, S2, S3 | Independent files (`mods.py` guard / `preflight.py` / `Mods.tsx` + `ModDetail.tsx`). Run all three in parallel. |
| 1 | S4, S5 | S4 shares `mods.py` with S1 → **after S1**. S5 needs S2's new field → **after S2**. S4 and S5 touch disjoint files → parallel with each other. |
| 2 | S6, S7 | S6 needs S4's route → after S4. S7 needs S5's component (logical) and shares `Mods.tsx` **and** `ModDetail.tsx` with S3 (file) → after both. S6 and S7 touch disjoint files → parallel with each other. |

---

## Phase 0

### S1 — Relax the soft-delete guard · §A1, fact #1

**Deliver.** In `delete_local_mod` (`backend/app/api/mods.py:398-439`), change:

```python
if guid in directly_referenced or guid in closure_owners:
```
to
```python
if guid not in directly_referenced and guid in closure_owners:
```

Update the function's docstring (402-408) to state that soft-delete is now allowed while a mod is
directly assigned to a server/modpack, and refused only when it's kept alive purely as another
mod's dependency. Leave `delete_library_mod` and every other check in `delete_local_mod` untouched:
running-server 409 (410-414), not-found 404 (416-417), not-`is_local` 409 (418-419), addon-dir must
resolve inside `addons_root()` 400 (428-434), then `shutil.rmtree` + `is_local=False` (436-438).

Also fix the 409 **message** on this path. `_delete_block_detail` (`mods.py:159-185`) phrases the
block in terms of servers/modpacks *and* parent mods; after this change the soft-delete route can
only ever block for the parent-dependency case, so its servers/modpacks half is dead, misleading
copy here. Give `delete_local_mod` a dependency-only detail (e.g. reuse `_delete_block_detail`'s
`closure_owners`-minus-self parent lookup and drop the servers/packs clause, or pass a flag).
`delete_library_mod` keeps the full message verbatim.

**Done when.** In `backend/tests/test_storage_and_download.py`: `test_delete_local_409_when_directly_referenced`
(line 224) is updated to expect `200`, `is_local: false`, the addon dir gone, **and** the
`ServerMod` row for `GUID_A` still present (assert via a session query). Add one new case: a mod
that is *both* directly assigned to server X *and* a resolved dependency of another mod assigned
to server Y — soft-delete must still succeed (it's in `directly_referenced`). Run the full file —
`test_delete_local_409_for_kept_dependency` (233) must still pass unchanged (409) — extend it to
assert the detail names the parent mod and does **not** mention servers/modpacks.

Both fixtures are known-good for this: test 1 seeds mod A `is_local`, `_assign(A)` (creates
`Server` + `ServerMod`) and an addon dir, so flipping it to 200 / dir-gone / `ServerMod` surviving
works as written — `delete_local_mod` never touches `ServerMod`, and the same pattern already
returns 200 in `test_delete_local_happy_path_keeps_row`.

**Watch out.** `directly_referenced ⊆ closure_owners` always (a root is in its own closure) — the
bug to avoid is writing `guid not in closure_owners` alone, which would wrongly allow soft-delete
for a mod that isn't referenced *at all* today too (already allowed, harmless) but reads as if it
changes that path — keep the condition exactly as given so the "still just a dependency" case
stays blocked.

### S2 — Preflight reports local disk state · §B3, fact #7

**Deliver.** In `preflight()` (`backend/app/servers/preflight.py`), the per-node `detail` dict
(built ~line 121-127, appended to `resolved_mods` at line 218) gains one field:
`detail["local"] = bool(mod is not None and mod.is_local)`. `mod` is already the looked-up
`Mod | None` for this guid (line 117) — no new query, and it is never reassigned before the append.

**No backend schema change.** `resolved_mods` is untyped server-side: `PreflightReport.resolved_mods`
is a `list[dict]` dataclass field (`preflight.py:49`) and the route (`servers.py:411-414`) is `-> dict`
returning `.as_dict()` with no `response_model`. The only type to widen is the frontend one (S5).

**Done when.** A `test_preflight.py` (or `test_preflight_blocked.py`) case: a resolved mod with
`is_local=True` reports `local: true`; one with no `mods` row, or `is_local=False`, reports
`local: false`. No other field in `resolved_mods` changes shape. `test_preflight.py` already seeds
both an `is_local=True` (line 74) and an `is_local=False` (line 206) mod — extend those rather than
building a new fixture; `test_preflight_blocked.py` inserts no `Mod` row at all, which is the
`mod is None → local: false` case.

**Watch out.** This is additive only — do not touch `availability` (Workshop-resolvability) or the
missing-size aggregation (206-210); `local` is a new, separate field, not a replacement for either.

### S3 — One clear, consistent delete/soft-delete UX, everywhere it exists · §A2, facts #10-11

Two files offer this today (fact #11, grep-confirmed exhaustive): `Mods.tsx` (both actions) and
`ModDetail.tsx` (full delete only, weaker flow). This story makes them the same experience.

**Deliver.**
0. **Line refs, verified**: `Mods.tsx` mutations 304-319 (`removeLocalMutation`) / 320-334
   (`removeLibraryMutation`), `refTarget` + live references query 245-250, `renderReferences()`
   **446-486** (not 475), dialogs 893-925 / 926-962 with inline `deleteError` at 905 / 942.
   `ModDetail.tsx` action cluster **268-296** — it already renders five buttons including a
   "Remove from library" full-delete (292-294), so the disk-only action below is an addition to
   that cluster, not a "third button".
1. **Rename**, in both files: disk-only action/button/dialog-title → "Delete downloaded files";
   full-delete action/button/dialog-title → "Delete mod entirely". Body copy states plainly that
   disk-only keeps the entry and every reference and is refetched automatically on next start
   (once S1/S4/S6 ship), while full delete is refused by any reference and needs the mod re-added
   by Workshop URL/ID afterward. Exact wording is adjustable; the distinction must survive.
2. **Prominent live warning, both files.** In `Mods.tsx`, redesign how a non-empty reference set is
   shown in both dialogs (disk 893-925, library 926-962): today `renderReferences()` (446-486)
   renders it as a muted `text-[11px] border-stone-700 bg-stone-900/60 text-stone-300` block (:464)
   regardless of severity. When `referencesQuery.data` has any `servers`/`modpacks`/`required_by`
   entries, switch that block to the `Badge tone="bad"` palette
   (`border-red-800 bg-red-950/50 text-red-300`, full-size text) and render it **above** the
   static paragraph, not below. Keep the "nothing references this mod" (emerald) and loading/error
   states as they are. In `ModDetail.tsx`, add the equivalent: a live query against
   `GET /api/mods/{guid}/references` (same shape `Mods.tsx` already uses) driving the same
   `tone="bad"` block in the delete dialog (513-554), replacing the current static
   `detail.required_by`-only paragraph (528-534) — this also fixes modpack usage being invisible
   there today (fact #11).
3. **Fix `ModDetail.tsx`'s error handling.** `deleteMutation`'s `onError` (205-217, specifically
   line 214) currently calls `setDeleteOpen(false)` — the dialog closes on failure and the reason
   only reaches a page-level `notice`. Change it to match `Mods.tsx`'s pattern: keep the dialog
   open, show the error inline inside it, same as `deleteError` in `Mods.tsx`.
4. **Add the missing disk-only action to `ModDetail.tsx`.** Mirror `Mods.tsx`'s `removeLocalMutation`
   (`DELETE /api/mods/${guid}/local`) and its dialog — same renamed copy, same live reference
   block, same open-on-error behavior. The action cluster (268-296) gains one more button alongside
   Re-download / Verify / Pin / Remove-from-library, disabled when `!detail.is_local` (the backend
   409s on a non-local soft-delete anyway, `mods.py:418-419`).

**Done when.** `npm run build` passes. Both files use the same wording for the same two actions;
both dialogs show the same live, prominent warning shape; a failed delete in either file leaves
its dialog open with the reason visible inline; `ModDetail.tsx` can soft-delete a directly-assigned
mod's files without leaving the page.

**Watch out.** `refTarget` (`Mods.tsx:245`) already unifies `diskTarget ?? libraryTarget` and
drives the references query there — don't add a second query or new state in `Mods.tsx`, only
restyle the existing render branch. In `ModDetail.tsx`, the new references query is net-new (there
isn't one today) — model it on `Mods.tsx`'s, don't reuse the page's existing `used_by`/
`required_by` fields for the dialog's warning (those are the incomplete, page-load-time data this
story is replacing).

---

## Phase 1

### S4 — `ensure-ready` endpoint + shared enqueue helper · §B4-5, facts #3-6

**Deliver.**
1. In `backend/app/mods/downloader.py`, add `async def enqueue_download_job(session, guids:
   list[str], versions: dict[str, str] | None = None) -> int`, containing the **tail** of
   `_enqueue_mod_download` (`mods.py:356-375`) — free-space guard (`ensure_sizes` +
   `estimate_download_bytes` + `check_free_space`) then
   `job_manager.enqueue(MOD_DOWNLOAD_JOB_KIND, params={"guids": upper_guids, "versions": versions or {}})`,
   returning the job id.
   **Do not move the 404 loop.** `_enqueue_mod_download` opens with a per-guid
   `raise HTTPException(404, f"mod not found: {guid}")` (`mods.py:367`). That stays in `mods.py`:
   `ensure-ready`'s missing predicate deliberately includes `row is None`, so a shared helper that
   404s on an unknown guid would break the new route's primary case. `mods.py`'s route keeps the
   404 loop, then calls the helper, then wraps in `JobEnqueuedOut` — behaviour unchanged, existing
   tests in `test_storage_and_download.py:104-141` must still pass untouched.
   No import cycle: `app/mods/__init__.py` loads `.sync` (which already imports `..core.jobs`)
   before `.downloader`, and `freespace.py` is already the one non-API module raising
   `HTTPException` — the helper only calls it.
2. New schema in `backend/app/schemas/server.py`: `ServerModsReadyOut(BaseModel)` with
   `job_id: int | None`, `kind: str | None`, `missing: list[str]`.
3. New route in `backend/app/api/servers.py`, next to `apply_server_updates` (417-422):
   `POST /{server_id}/mods/ensure-ready`, `response_model=ServerModsReadyOut`,
   `status_code=202`. Load the server with `server = await _load(session, server_id)`
   (`servers.py:82-90`) — **mandatory**, not stylistic: `resolved_mod_entries` does
   `sorted(server.mods, …)` (`config_gen.py:187`) and only `_load` eager-loads `Server.mods` via
   `selectinload`; a plain `session.get(Server, id)` raises `MissingGreenlet`. Then call
   `resolved_mod_entries(session, server)`,
   then compute `missing` by loading `Mod` rows for every `entry.mod_id` and keeping an entry when
   its row is `None`, or `not row.is_local`, or (`entry.version` is set and
   `row.installed_version != entry.version`) — mirror the predicate in `ensure_mods_ready`
   (`downloader.py:444-450`) exactly, just sourced from `ModEntry` instead of a raw guid list. If
   `missing` is empty, return `ServerModsReadyOut(job_id=None, kind=None, missing=[])` without
   enqueuing anything. Otherwise call `enqueue_download_job` with the missing guids and a
   `{guid: version}` map built from the missing entries' `.version`, and return the job id +
   `kind=MOD_DOWNLOAD_JOB_KIND` + the missing guid list.

**Done when.** New tests in `test_storage_and_download.py` (or a new `test_server_mods_ready.py`
following its fixture style): all mods local → `job_id: null`, no job row created; one mod not
local → job enqueued with that guid, `missing` lists it; a mod with a `ServerMod.pinned_version`
that differs from `installed_version` is included in `missing` with that pinned version (not
latest) in the job's `versions` param; free-space guard fires the same 409 as
`test_download_refused_when_projected_exceeds_free_space` (124) when projected size exceeds free
space.

**Watch out.**
- Do not resolve dependencies a second time with different parameters — call
  `resolved_mod_entries` itself (fact #3) so this can never disagree with what a real start would
  write to `config.json`.
- Do not import `_enqueue_mod_download` (leading underscore) from `servers.py` — the whole point of
  this story is the extraction in step 1.
- `ModEntry.version` is `None` when unpinned (`config_gen.py:53`), and the pin itself is
  `ServerMod.pinned_version` with a library-wide `Mod.pinned_version` fallback
  (`config_gen.py:251-255`) — build the `{guid: version}` map only from entries whose `.version` is
  truthy, mirroring `_enqueue_mod_download`'s `versions or {}`.
- A dependency that fails all three `loadable` conditions (`config_gen.py:238-242`) is dropped by
  `resolved_mod_entries` before this filter ever sees it, so `ensure-ready` cannot fetch it. That is
  expected — preflight is what reports it. Do not add a second resolution pass to "catch" it.

### S5 — Local-state badge + manual redownload in the mods panel · §C7-8 (partial), fact #7

**Deliver.** New `frontend/src/components/mods/LocalStateBadge.tsx`: given a mod's `is_local`
(or, in the preflight context, the new `local` field from S2) and whether it's referenced, render
one of: downloaded (existing look), "not cached — fetched automatically on next start" (new,
informational tone, not the red "bad" tone — this is normal/expected, not an error), or the
existing orphaned-and-unreferenced case unchanged. Include a small "Redownload now" button that
POSTs `/api/mods/{guid}/download` (`mods.py:387-395`, unchanged) and polls the returned job the
same way as `useInvalidateGraphOnJob` (`modGraph.ts:35-51`) — on completion, invalidate whatever
query the caller passes in (accept an `onDone` callback or a query key prop; keep it generic since
S7 reuses this in three more places).

The directory `frontend/src/components/mods/` already exists (`FreshnessPill.tsx`,
`ModLibraryPicker.tsx`, `ModTree.tsx`, `SortableModList.tsx`) — follow `FreshnessPill.tsx`'s shape.

Wire it into `ModsPanel.tsx`'s preflight render (section **509-588**, header "Deployment
pre-flight"): for each `resolved_mods` entry with `local: false`, show the badge + redownload
action. Widen the frontend `Preflight` type (`lib/api.ts:109-119`) to add `local?: boolean` to the
`resolved_mods` array element.

**Done when.** `npm run build` passes.

**Watch out — the existing grouped render is not a usable anchor.** `preflightGroups`
(`ModsPanel.tsx:140-162`) cannot carry this:
- `nameByGuid` (140-145) keeps only `{guid, name}` and discards the rest of each `resolved_mods`
  entry — `local`, `availability` and `version` are not reachable from a group.
- `order` (line 155) is populated **only** from checks that carry a `guid`, so a resolved mod with
  no flagged check never gets a group heading at all — exactly the common case for a soft-deleted
  but otherwise healthy mod.

So build a separate `guid → resolved_mods entry` map and iterate `resolved_mods` itself for the
badge row. Leave `preflightGroups` and the check rendering it drives untouched. (This reverses the
earlier "don't build a second list" note — that instruction was based on a wrong reading of the
memo.)

---

## Phase 2

### S6 — Ensure-ready-then-start on every start action · §B6, facts #8-9

**Deliver.** New `frontend/src/lib/serverStart.ts`:
`export async function startServerReady(id: number): Promise<void>`. Steps: `POST
/api/servers/${id}/mods/ensure-ready` (typed via a new `ServerModsReadyOut` type mirroring the
backend schema, added to `lib/api.ts`); if `job_id` is non-null, poll `GET /api/jobs/${job_id}`
every 2s (same interval as `useInvalidateGraphOnJob`) until `state` is one of
`succeeded|failed|cancelled`; on `failed`/`cancelled` throw an `Error` carrying the job's `error`
text; on `succeeded`, or when `job_id` was null to begin with, `POST /api/servers/${id}/start`.

Replace the bare `await api(\`/api/servers/${server.id}/start\`, { method: "POST" })` at
`ServerRow.tsx:32` with `await startServerReady(server.id)`. Do the same for both occurrences in
`SavesPanel.tsx` (276, 334), inside their existing `run(name, fn)` busy-state wrapper so the
existing busy indicator covers the download wait.

**Do not extend `run`.** It is not defined in `SavesPanel.tsx` — it is a prop (typed at
`SavesPanel.tsx:144`), implemented in `ServerDetail.tsx:36-48`, and it captures its label once via
`setAction(name)` before awaiting. A changing label ("preparing mods…" → "starting…") would need a
signature change affecting every other server action. Instead pass **one static label** covering the
whole sequence (e.g. `run("Preparing mods & starting…", () => startServerReady(id))`).

**Done when.** `npm run build` passes.

**Watch out.** `ServerRow.tsx`'s `power` mutation already has a pre-start `window.confirm` +
stop-the-other-server branch (21-38) for the single-server rule — `startServerReady` replaces only
the final start call, the confirm/stop sequencing stays exactly as-is and runs *before* it.

### S7 — Surface local state on the library, detail, and modpack views · §C8

**Deliver.** Reuse `LocalStateBadge` (S5) in three more places:
- `Mods.tsx` library list/table: alongside the existing `is_orphan`/`kept_by` rendering, add the
  badge for any row where `is_local` is false but the mod is referenced (directly or via closure).
  The `refs=true` list fetch is expected to already carry `is_local` + `is_orphan`/`kept_by` — this
  one was not re-verified; confirm before adding any new query, and if a field is genuinely absent,
  widen the list response rather than fetching per row.
- `ModDetail.tsx`: same badge near wherever the page currently shows `is_local`/pin info.
- `Modpacks.tsx`: same badge per mod row in a pack's item list.

**Done when.** `npm run build` passes.

**Watch out.** This story starts only after S3 has landed in `Mods.tsx` (same file, sequential) —
rebase onto that diff rather than editing in parallel. Don't duplicate the "not cached" logic
inline in three files; every call site renders the same component from S5.

---

## What's deliberately not a story

Backend guard/route work only ever narrows or extends existing checks — no new tables, no new job
kinds, no change to `mod_sync`'s reconciliation pass or to `ensure_mods_ready`. If a story here
seems to need touching either of those, stop and re-read PLAN.md's Non-goals before proceeding.
