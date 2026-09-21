# Reforger Manager — Sprint 14: mod soft-delete + start-time auto-provision

> Implementation phases live in [STORIES.md](STORIES.md).

## Context

A mod deleted from the library today either keeps its files (`DELETE /mods/{guid}/local`,
already exists) or is forgotten entirely (`DELETE /mods/{guid}`) — but both routes refuse
whenever the mod is still referenced by a server, a modpack, or another mod's dependency
closure. There is no way to reclaim a referenced mod's disk space without first stripping every
reference. Separately, a session review of the live delete-confirmation modal found the
blocking-reference detail (`_delete_block_detail`, already computed server-side) rendered as a
barely-visible `text-[11px]` note — easy to miss, which is what prompted this sprint.

Investigation (see repo conversation, 2026-09-21) found most of the machinery already exists:
`is_local` is already the disk-presence flag; a server writes every explicitly-assigned mod into
its generated config regardless of `is_local` (`config_gen.py`); the real Reforger engine
self-downloads any addon named in that config on boot; and the `mod_download` job already
reconciles `is_local` back to `true` after a successful fetch (`refresh_local_mods`). The gap is
narrower than "build a new download system": relax one guard, add one pre-start step that
reuses the existing job, and make the existing states visible in the UI.

### Guiding principle

*Soft-delete only ever removes cached bytes, never knowledge.* A mod's row, dependency edges,
scenarios, and every reference to its GUID survive a soft-delete unconditionally. Nothing new is
invented for re-fetching it — every redownload path (start-time, manual) goes through the one
existing job (`mod_download`) that already verifies the full dependency closure landed on disk
and flips `is_local` back. The only genuinely new backend code is the guard relaxation and a
thin endpoint that decides *what* to feed that job before a server starts.

## Decisions locked for this sprint

| Decision | Choice |
|---|---|
| Redownload trigger | **Blocking pre-start job.** Starting a server first checks its resolved mod set for anything not local, runs the existing batch-download job (progress-visible) to completion, *then* starts. Never a bare, silent engine-native fetch. |
| Soft-delete scope | **Direct references only.** A mod directly assigned to a server or modpack can be soft-deleted while referenced. A mod that is *only* present as another mod's resolved dependency stays under the current strict guard. |
| "No feedback on blocked delete" | Already correct server-side and already rendered client-side on `/mods` — but too subtle, and `ModDetail.tsx` runs a second, weaker copy of the same flow (fact #11). **Redesign the warning to be unmissable, and make it the same warning, everywhere a delete/soft-delete action exists** — grepped to exactly two files (fact #11) — not a new capability. |
| Naming | Both actions are renamed for clarity, everywhere they appear: disk-only becomes **"Delete downloaded files"** (keeps the library entry and every reference; refetched automatically next time a server that needs it starts), full delete becomes **"Delete mod entirely"** (drops the entry, its dependency/scenario/pin records, and its files). Exact copy is adjustable at implementation time; the distinction each label must carry is not. |
| Frontend depth | Full: a shared "not cached locally" badge + manual "Redownload now" action, surfaced in the mods library, mod detail, server mods panel, and modpacks. |
| Full `DELETE /mods/{guid}` guard | **Unchanged.** Still refuses on any reference, direct or closure. `ServerMod.mod_guid` / `ModpackItem.mod_guid` are plain indexed strings, not FKs — forgetting a mod's row while something still points at its GUID is a real dangling reference, not a state to enable. |
| `ensure_mods_ready` (`downloader.py:428`) | **Not reused.** It fires `_run_engine` directly with no closure-verification/retry and no `is_local` reconciliation — weaker than the real job path. Left as-is; out of scope. |

## Code-level facts the stories must respect

1. **The guard to change** is `delete_local_mod` (`backend/app/api/mods.py:398-439`). It currently
   blocks when `guid in directly_referenced or guid in closure_owners`
   (`orphan_reference_sets`, `backend/app/api/storage.py:50-72`). `closure_owners` includes every
   root's own GUID (a root is in its own closure — see that function's docstring), so
   `directly_referenced ⊆ closure_owners` always holds. **Verified:** `resolve_dependencies` pops
   each root and appends it to `tree.nodes` unconditionally (`resolve.py:220-222`), *before* any
   branching and even when the `Mod` row is `None` — so `closure_owners.setdefault(root, …).add(root)`
   (`storage.py:71`) always fires and the subset holds in every case; the old guard's `or` was
   logically redundant. The new condition is: block only when
   `guid not in directly_referenced and guid in closure_owners` — i.e. still protect a *pure*
   transitive dependency, allow a direct root. `delete_library_mod` (`mods.py:442-479`) keeps its
   condition unchanged.
   **Consequence for the 409 message:** after this change `delete_local_mod` can only ever 409 for
   the dependency-only case, but `_delete_block_detail` (`mods.py:159-185`) still phrases it in terms
   of servers/modpacks *and* parents. Its servers/modpacks half becomes dead, misleading copy on
   this path — the soft-delete 409 must read as "kept alive as a dependency of X" (see §A1). The
   full-delete path keeps the message unchanged.
2. **Dependency edges are DB-first, not disk-first.** `resolve_dependencies`
   (`backend/app/mods/resolve.py:114-239`) reads persisted `mod_dependencies` rows
   (`_gproj_children`) before ever falling back to live `addon.gproj` parsing, and that fallback
   only fires when `use_disk=True` *and* no persisted row exists. `orphan_reference_sets` and
   `config_gen`'s resolution both pass `use_api=False` — closure computation for the delete guard
   and for a running server never depends on a mod's files being present. Soft-delete is safe
   against this: it doesn't blind the resolver for anything that was ever successfully scanned.
3. **`resolved_mod_entries`** (`backend/app/servers/config_gen.py:162-259`) is the single source
   of truth for what a server start will request. For an explicitly-assigned mod (`sm is not
   None`, from `ServerMod`) it is written into the entry list unconditionally — no `is_local`
   check. For a dependency-only node (`sm is None`) it is gated by `loadable` (line 238-242:
   local, or `api_state == "ok"`, or resolver state `"ok"`) and silently dropped otherwise. Any
   new "what's missing" computation must call this function and diff its output against
   `Mod.is_local` — not re-resolve independently — so it can never disagree with what actually
   gets written to `config.json`.
   Two consequences, both intended:
   - A soft-deleted *dependency* (`is_local=False`) with `api_state == "ok"` still passes `loadable`
     and is still emitted — so the missing-diff sees it and refetches it. Correct.
   - A dependency that fails **all three** `loadable` conditions is dropped before the diff ever
     sees it, so `ensure-ready` can never fetch it. That is not a regression and not a bug to fix
     here: preflight is the thing that reports such a mod (`config_gen.py:176-179` docstring).
   **Eager-load requirement:** `resolved_mod_entries(session, server)` takes the loaded `Server`
   object and does `sorted(server.mods, …)` (line 187). It therefore requires
   `selectinload(Server.mods)` — which `_load` (`backend/app/api/servers.py:82-90`) already does. A
   plain `session.get(Server, id)` raises `MissingGreenlet` at runtime. Any new caller must go
   through `_load`.
4. **The reconciliation already exists.** `_job_mod_download`
   (`backend/app/main.py:239-276`), the registered handler for `MOD_DOWNLOAD_JOB_KIND`
   (registered `main.py:310`): runs the headless download, verifies the *full* expected
   dependency closure landed on disk via `expected_closure` + `partition_present`
   (`downloader.py:398-425`, retrying missing ones once, failing loud if still missing), then
   calls `refresh_local_mods(guids)` (`backend/app/mods/sync.py:442-462`) to flip `is_local=True`
   and pick up on-disk size/version immediately — no separate rescan needed. Routing a pre-start
   download through this job (not `ensure_mods_ready`, not a bare `_run_engine` call) gets this
   for free.
5. **The free-space guard is currently mods.py-private.** `_enqueue_mod_download`
   (`mods.py:356-375`) does `ensure_sizes` + `estimate_download_bytes` +
   `check_free_space(settings.mods_dir, projected)` (`mods/freespace.py`) before
   `job_manager.enqueue(MOD_DOWNLOAD_JOB_KIND, params={"guids": ..., "versions": ...})`. This needs
   to be reachable from `app/api/servers.py` too — extract it into `app/mods/downloader.py` as a
   shared function; do not import a leading-underscore name across modules.
   **Extract the tail only.** `_enqueue_mod_download` *starts* with a per-guid
   `raise HTTPException(404, f"mod not found: {guid}")` loop (`mods.py:367`). That loop must stay in
   the `mods.py` route wrapper and must **not** move into the shared helper — `ensure-ready`'s
   missing predicate deliberately includes `row is None` (a mod with no DB row is exactly a thing it
   must download), so a helper that 404s on an unknown guid would break the new route's main case.
   Extract only `ensure_sizes` → `estimate_download_bytes` → `check_free_space` → `enqueue`.
   **No import cycle** (verified): `app/mods/__init__.py` imports `.sync` before `.downloader`, and
   `sync.py` already imports `..core.jobs`, so `core.jobs` and `.freespace`/`.workshop` are all
   loaded by the time `downloader.py` executes. `check_free_space` raising `HTTPException(409)` from
   a non-API module is already the status quo — `freespace.py` is the only such module today, and
   the helper merely calls it.
6. **`JobManager` is strictly single-worker** (`backend/app/core/jobs.py:65-163`): one
   `asyncio.Queue`, one worker task, jobs run one at a time in enqueue order. Two servers'
   pre-start downloads queued back to back is safe by construction — no new locking needed.
7. **Precedent for the new route**: `apply_server_updates`
   (`backend/app/api/servers.py:417-422`) is the existing shape for "server-scoped action that
   enqueues a mod job" — `_load(session, server_id)`, a guard call, `job_manager.enqueue`, return
   a job-shaped schema. `preflight`'s `resolved_mods` entries (`servers/preflight.py`, the `detail`
   dict built ~line 121-127) do not currently carry a local/missing flag — add one (`detail["local"]
   = mod.is_local if mod else False`) so the frontend can show "not cached" without a second call.
   `mod = library_mods.get(guid)` is already in scope at line 117 and is never reassigned before the
   `resolved_mods.append(detail)` at line 218 — no new query. **No backend schema change:**
   `resolved_mods` is untyped end-to-end server-side (`PreflightReport.resolved_mods: list[dict]`, a
   dataclass field at `preflight.py:49`; the route `servers.py:411-414` is `-> dict` and returns
   `.as_dict()` with no `response_model`). The only typed surface is the frontend `Preflight` type.
8. **Job polling precedent (frontend)**: `useInvalidateGraphOnJob`
   (`frontend/src/lib/modGraph.ts:35-51`) is the existing react-query pattern — poll
   `GET /api/jobs/{id}` on a 2s `refetchInterval` until `state` is terminal
   (`succeeded|failed|cancelled`). The new start-flow needs an *awaitable* version of this (called
   from imperative mutation functions, not a hook) with the same terminal-state set.
9. **Three call sites currently call `/start` directly**, all needing the same "ensure ready
   first" wrapper: `ServerRow.tsx`'s `power` mutation (`ServerRow.tsx:21-38`, line 32),
   `SavesPanel.tsx:276` (restart + load a specific save) and `SavesPanel.tsx:334` (restart fresh).
   Grep-confirmed exhaustive: these are the only three `/start` calls in `frontend/src`.
   Do not triplicate the sequencing — one shared helper, three call sites updated.
   **`run` is not defined in `SavesPanel.tsx`** — it is a prop (typed at `SavesPanel.tsx:144`),
   implemented in `ServerDetail.tsx:36-48`, and it captures its label once via `setAction(name)`.
   It therefore cannot show a *changing* label ("preparing mods…" → "starting…") without changing a
   signature shared by every other server action. Do not extend it: pass one static label that
   covers the whole sequence.
10. **Existing danger styling to reuse**: `Badge tone="bad"` (`frontend/src/components/ui.tsx:64-82`,
    `border-red-800 bg-red-950/50 text-red-300`) is the only "this is blocking/dangerous" visual
    precedent in the app. The current blocking-reference block in the delete dialogs
    (`Mods.tsx`, `renderReferences()` lines 446-486, rendered at 893-962) is `text-[11px]
    border-stone-700 bg-stone-900/60 text-stone-300` — visually indistinguishable from
    informational text. That mismatch is the actual bug to fix.
11. **`ModDetail.tsx` has a second, weaker copy of the delete flow — grep confirms these are the
    only two files that call `DELETE /mods/{guid}` or `DELETE /mods/{guid}/local`.** Its
    `deleteMutation` (`ModDetail.tsx:205-217`) calls `setDeleteOpen(false)` inside `onError`
    (line 214) — the confirm dialog **closes on a failed delete**, and the reason surfaces only as
    a page-level `notice`, unlike `Mods.tsx` which keeps the dialog open with the error inline.
    Its dialog (513-554) shows only `detail.required_by` (reverse dependencies); server usage is
    shown separately, always-on, in a "Used By" card (479-510) sourced from `detail.used_by` —
    which the backend's `get_mod_detail` populates from **servers only** (a plain
    `Server`/`ServerMod` join), no modpack query at all, so modpack usage is invisible anywhere on
    this page today. It also has no disk-only delete action — only the full-delete route (206).
    The page's action-button cluster is **268-296** and already holds five buttons, including a
    "Remove from library" full-delete at 292-294 — the disk-only action is an *addition to* that
    cluster, not "a third button".

## Feature set

### A. Soft-delete guard + a single, clear delete/soft-delete UX

1. Relax `delete_local_mod`'s guard per fact 1. `delete_library_mod` untouched. Adjust the 409
   message on the soft-delete path so it names the *dependency* reason only (fact 1) — the
   servers/modpacks phrasing can no longer occur there.
2. One consistent delete/soft-delete experience across both files that offer it (fact #11):
   - **Rename both actions and their dialogs** per the Naming decision above — button labels,
     dialog titles, and body copy, in both `Mods.tsx` and `ModDetail.tsx`.
   - Both dialogs, in both files, show the live `GET /mods/{guid}/references` result (servers,
     modpacks, dependents) as a prominent `tone="bad"`-styled block, placed above the generic
     explanatory paragraph, whenever it's non-empty — not just on a failed delete attempt, and not
     just on `/mods`. This also fixes `ModDetail.tsx`'s missing modpack visibility (fact #11), since
     `/references` already returns modpacks and `ModDetailOut.used_by` does not.
   - Fix `ModDetail.tsx`'s error handling to match `Mods.tsx`: keep the dialog open on a failed
     delete, show the error inline — do not close it (fact #11).
   - Add a disk-only ("Delete downloaded files") action to `ModDetail.tsx`, for parity with
     `Mods.tsx` — today it only offers the full delete.
   - Copy must make clear the two actions aren't in conflict: full delete is refused by any
     reference (unchanged); disk-only is now allowed whenever the mod is directly assigned.

### B. Start-time mod provisioning

3. `preflight.py`: add `local: bool` to each `resolved_mods` entry (fact 7).
4. Extract `_enqueue_mod_download`'s **tail** (sizes → estimate → free-space → enqueue) into
   `app/mods/downloader.py` as `enqueue_download_job(session, guids, versions=None) -> int`
   (returns `job_id`); the per-guid 404 loop stays behind in `mods.py` (fact 5). `mods.py`'s route
   becomes a thin wrapper: 404 loop, then the helper, then `JobEnqueuedOut`.
5. New `POST /api/servers/{server_id}/mods/ensure-ready`: load the server **via `_load`** (fact 3 —
   `resolved_mod_entries` needs `Server.mods` eagerly loaded), call
   `resolved_mod_entries`, filter to entries that are missing / not local / version-pin-mismatched
   (mirror the predicate in `ensure_mods_ready`, `downloader.py:444-450`, applied to
   `ModEntry.mod_id`/`.version` instead of a raw guid list). Empty → `{job_id: null, missing: []}`.
   Non-empty → call `enqueue_download_job` with the missing guids + their pinned versions, return
   `{job_id, missing: [guids]}`. New schema `ServerModsReadyOut` in `schemas/server.py`.
6. Frontend: one awaitable helper (`lib/serverStart.ts`) — POST ensure-ready, poll any returned
   job to terminal (fact 8), throw with the job's `error` on failure, then POST `/start`. Wire into
   all three call sites from fact 9, showing a busy state for the duration — one static label via
   the existing `run(name, fn)` prop, no change to its signature (fact 9).

### C. Frontend state surfacing

7. One shared component (e.g. `components/mods/LocalStateBadge.tsx`) rendering: downloaded /
   not cached, will auto-fetch on next start / orphaned-and-unreferenced (existing concept,
   unchanged) — plus a "Redownload now" action that POSTs the existing
   `/api/mods/{guid}/download` (`mods.py:387-395`, unchanged) and polls it the same way as B6.
8. Apply it: `Mods.tsx` library list (alongside the existing `is_orphan`/`kept_by` badges),
   `ModDetail.tsx`, `ModsPanel.tsx` (next to the preflight panel, using the new `local` field from
   B3), `Modpacks.tsx`.
   **`ModsPanel.tsx` caveat:** the existing `preflightGroups` memo (`ModsPanel.tsx:140-162`) is not
   a usable anchor for this. It keeps only `{guid, name}` in `nameByGuid` and discards the rest of
   each `resolved_mods` entry, and its `order` (line 155) is populated *only* from checks that carry
   a guid — a resolved mod with no flagged check gets no heading at all. The badge therefore needs
   its own `guid → resolved_mods entry` map, iterating `resolved_mods` directly. The preflight
   render block is lines **509-588**.

## Data-model changes

None. `Mod.is_local` already exists and already carries the soft-delete-state meaning.

## Testing

Backend (proportionate, in the style of `backend/tests/test_mod_refs.py`,
`test_preflight.py`, `test_batch_mod_download.py`):
- Guard: soft-delete succeeds for a directly-assigned mod while referenced; still refused for a
  mod that is only a resolved dependency of another referenced mod, with a 409 detail that names
  the *dependency* reason (fact 1); full delete still refused in both cases, message unchanged.
- Preflight: a resolved entry for a non-local mod carries `local: false`; a local one `true`.
- `ensure-ready`: nothing missing → `job_id: null`; something missing → job enqueued with the
  exact missing guids and their pinned versions (not latest); free-space guard still 409s the
  same way the existing download route does when the projected size exceeds free space.

Frontend: `npm run build` only, per house rules.

## Cut from this sprint / Non-goals

- Soft-delete for dependency-closure-only mods (locked decision, table above).
- Any change to `ensure_mods_ready`, or to `mod_sync`'s existing prune-pass reconciliation
  (`sync.py:512-519`) — both orthogonal and already correct.
- Bulk "soft-delete everything unused" tooling — one mod at a time, via the existing dialog.
- Any TrueNAS/production change. Live verification is local Docker only.

## Verification

- `cd backend && .venv/Scripts/python.exe -m pytest -q`
- `cd frontend && npm run build`
- Manual (local stack, chrome-devtools MCP): soft-delete a directly-referenced mod's files, confirm
  the library row/server assignment survive; open the delete modal for a still-blocked
  (dependency-only or full-delete) case **on both `/mods` and a mod's own detail page** and confirm
  the warning is prominent, lists modpacks too, and the dialog stays open with the error inline on
  a failed attempt; start the server that references the soft-deleted mod and watch the
  ensure-ready job run and `is_local` flip back true; confirm a pinned mod redownloads its pinned
  version, not latest.
