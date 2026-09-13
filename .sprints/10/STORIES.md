# Reforger Manager — Sprint 10 stories

> Context, code anchors and every verified fact: [PLAN.md](PLAN.md). Line numbers
> verified 2026-09-13 — trust the symbol name if a line has drifted.

## Shared reference (read before starting any story)

| Thing | Where | Notes |
|---|---|---|
| Save tree | `PROFILES_DIR/{id}/profile/.save/game/{GUID}-{name}/playthrough{NNN}/savepoint{NNN}/` | Note the `profile/` level. Contains `meta-info.json` + `WorldState/` (opaque, one `.blob`). |
| Non-save sibling | `.save/settings/` | Never list, snapshot or delete. A server with no `.save/game` is normal. |
| Save metadata | `meta-info.json` | Fields + a real example: PLAN F-fact 4. `m_Id` = the UUID. `m_aUsedAddons` is **empty** — do not use it. |
| Path safety | `backend/app/servers/files.py` — `resolve_safe_path()` `:112-132`, `EXCLUDED_TOP_LEVEL` `:36` | Every save path goes through it. `Path(base) / "/abs"` discards base. |
| Running guard | `backend/app/api/server_files.py` — `_refuse_while_running()` `:59-63` | Reuse verbatim; 409. |
| Config build | `backend/app/servers/config_gen.py` — `build_config()` `:64`, `_deep_merge()` `:111-116`, mods `:89`, `ModEntry.to_dict()` `:55-61` | `extra_config` merges **last**; keep that. |
| Launch argv | `backend/app/servers/supervisor.py:174-189` | Append save flags here. |
| Start + revisions | `backend/app/servers/supervisor.py` — `start()` `:116-229`, revision insert `:144-159` | Revision row written on **every** start, unconditionally. |
| Revision model | `backend/app/models/server.py:115-137` | `snapshot` = whole config dict; `created_at` tz-aware; never pruned. |
| Server model | `backend/app/models/server.py:28-137` | `Server`, `ServerMod` (`:90-112`, current state only), `ServerConfigRevision`. |
| Backup fields | `backend/app/api/backup.py` — `SERVER_CONFIG_FIELDS`, `export_backup` `:61-98` | New config columns must be added or they're dropped on export/import. |
| Free space | `backend/app/mods/freespace.py` | Reuse before snapshot/restore. |
| Test harness | `backend/tests/test_server_files_routes.py:33-64` | Copy verbatim. No `conftest.py`. In-memory sqlite, `patch.object(settings, "profiles_dir", tmp)`. |
| Tabs | `frontend/src/pages/ServerDetail.tsx:14-15, 131-153` | `tabs` const array + conditional render. |
| Panel model | `frontend/src/components/server/FilesPanel.tsx` | Query `:52-55`, ladder `:308-417`, running banner `:222-226`, confirm-delete `:127-131, 379-409`, Dialog `:419-475`. |
| API primitives | `frontend/src/lib/api.ts` — `api` `:168-179`, `apiVoid` `:182-192`, `apiUpload` `:195-209`, `apiBlob` `:212-222`, `saveBlob` `:225-234`; types near `:133-153` | No new wrapper needed. |
| UI primitives | `frontend/src/components/ui.tsx` | `Button`, `Card`, `Input`, `Badge`, `Dialog`. `Empty` from `components/Empty.tsx`. **No toast system** — inline `<p className="error">`. |
| MCP tools | `backend/app/mcp/tools.py` | Declarative `ToolSpec` table, mirrors REST 1:1, hand-maintained. |

Verify: `cd backend && .venv/Scripts/python.exe -m pytest -q` · `cd frontend && npm run build`

## Shape

| Story | Agent | Files | Depends on |
|---|---|---|---|
| S1 discovery + drift | claude | `servers/saves.py`, `schemas/saves.py`, `tests/test_saves_discovery.py` | — |
| S2 columns + migration | deepseek | `models/server.py`, `migrations/versions/0005_*.py`, `schemas/server.py`, `api/backup.py` | — |
| S3 persistence config | glm | `servers/config_gen.py`, `tests/test_config_gen_persistence.py` | S2 |
| S4 supervisor flags | claude | `servers/supervisor.py`, `tests/test_supervisor_save_selection.py` | S2 |
| S5 snapshot store | claude | `servers/save_snapshots.py`, `tests/test_save_snapshots.py` | S1 |
| S6 API router | deepseek | `api/server_saves.py`, `main.py`, `tests/test_saves_routes.py` | S1, S4, S5 |
| S7 MCP tools | glm | `mcp/tools.py` | S6 |
| S8 Saves tab | claude | `components/server/SavesPanel.tsx`, `lib/api.ts`, `pages/ServerDetail.tsx` | S6 |
| S9 persistence form | glm | `components/server/ServerForm.tsx` | S2 |
| S10 grace audit + results | deepseek | `RESULTS.md` (+ maybe one default) | all |

**Parallelism.** S1 ∥ S2 start together. Then S3 ∥ S4 ∥ S9 (all S2-only), and S5 (S1-only). S6 joins S1+S4+S5. S7 ∥ S8 after S6. S10 last.
**Serialise:** S2 → S3/S4/S9 (shared model). S5 → S6 (shared service). Nothing else shares a file.

---

## S1 — save discovery + mod drift

Create `backend/app/servers/saves.py` and `backend/app/schemas/saves.py`. Read-only; no writes, no routes.

**Deliver.**

1. `discover(session, server) -> list[ScenarioSaves]`. Glob
   `profile/.save/game/*/playthrough*/savepoint*/meta-info.json` under the server's
   profile root, plus a bounded fallback glob for `meta-info.json` at depth ≤ 6
   under `.save/`; dedupe by resolved path.
2. Parse each `meta-info.json` defensively — unreadable/invalid JSON still yields a
   record with `readable: false`, never dropped.
3. Group scenario dir → playthrough (`m_iPlaythroughNr`) → save point
   (`m_iSavePointNr`), each **descending**. Order from the parsed numbers, not dir
   names.
4. Per save point: `uuid` (`m_Id`), `saved_at`, `playtime_seconds`, `game_version`,
   `size_bytes` (recursive sum), `matches_current_scenario`
   (`m_sMissionResource == server.scenario_id`, exact string compare),
   `engine_drift` vs the installed engine build.
5. Mod drift: load the server's `ServerConfigRevision` rows ordered by `created_at`;
   per save point pick the newest with `created_at <= saved_at`; diff
   `snapshot["game"]["mods"]` **`modId` set** against current `ServerMod` ids. Emit
   `mod_drift: {added: [names], removed: [names]} | null`, or
   `mod_drift_unknown: true` when no revision precedes the save.
6. Skip `.save/settings` and anything not matching `savepoint*`.

**Done when.** `pytest -q` green with PLAN test cases 1, 2, 3, 4, 13 — no more.

**Watch out.**
- Scenario dir name is **not** derivable from `scenario_id` (braces stripped, `_`→`-`); discover by glob, match by `m_sMissionResource`.
- `mod_drift_unknown` must not collapse into "no drift".
- Compare ids, display names. Never assert a version changed — `version` is absent for unpinned mods.
- One query for revisions and one for `ServerMod`, outside the per-save loop.

---

## S2 — persistence + selection columns

**Deliver.**

1. Nine columns on `Server` (`backend/app/models/server.py`), all with server-side defaults: `persistence_enabled` bool `true`, `auto_save_interval` int `10`, `save_retention` int `10`, `load_session_save` bool `true`, `keep_session_save` bool `false`, `hive_id` int `0`, `save_mode` str `'latest'`, `save_pinned_uuid` str nullable, `save_selection_sticky` bool `false`.
2. Alembic revision `0005_server_persistence_and_save_selection`, down_revision = `0004_server_game_admins`. Both `upgrade()` and `downgrade()`.
3. Pydantic validation in `backend/app/schemas/server.py`: `auto_save_interval` 0–60, `save_retention` 1–128, `hive_id` 0–16383, `save_mode` ∈ {`latest`,`pinned`,`fresh`}.
4. Add the **six persistence** columns to `SERVER_CONFIG_FIELDS` in `backend/app/api/backup.py`. The three `save_*` selection columns are runtime state — **exclude** them.

**Done when.** `pytest -q` green; existing server rows still load.

**Watch out.** Use `server_default=` not just `default=`, or existing rows get NULLs. `DB_MIGRATE_ON_STARTUP` defaults to `create_all`, so the migration must also be consistent with the model for fresh DBs.

---

## S3 — emit `game.gameProperties.persistence`

**Deliver.** In `build_config()`, emit inside `gameProperties` (sibling of `missionHeader`, **not** `game.persistence`):

```python
"persistence": {
    "autoSaveInterval": server.auto_save_interval,
    "saveRetention": server.save_retention,
    "loadSessionSave": server.load_session_save,
    "keepSessionSave": server.keep_session_save,
    "hiveId": server.hive_id,
}
```

When `persistence_enabled` is false, emit `missionHeader.m_eSaveTypes = 0` instead of the `persistence` block.

**Done when.** PLAN test case 5 passes — exact nesting, disabled path, and `extra_config` still winning the merge. Exactly that one test file.

**Watch out.** `_deep_merge(extra_config)` runs last — do not reorder. Do not touch `ModEntry.to_dict()` or the mods list (PLAN non-goal: stamping versions silently pins mods).

---

## S4 — supervisor save flags + consumption

**Deliver.**

1. In the argv block (`:174-189`), append by `server.save_mode`: `latest` → **nothing**; `pinned` → `-loadSessionSave <save_pinned_uuid>`; `fresh` → `-backendFreshSession`.
2. After the process spawns, in the same transaction that sets `is_running`: if `save_selection_sticky` is false and mode ≠ `latest`, reset `save_mode='latest'`, `save_pinned_uuid=None`.
3. If mode is `pinned` but the uuid is no longer on disk at start time: log a warning, fall back to `latest`, clear the selection — **do not fail the start**.

**Done when.** PLAN test case 6 (consume vs sticky) plus the vanished-uuid fallback. Assert argv by patching the spawn, not by launching anything.

**Watch out.** `latest` must append nothing so today's behaviour is byte-identical. Reset only after a *successful* spawn. Respect the existing `asyncio.Lock` / `SingleServerError` flow.

---

## S5 — snapshot store

Create `backend/app/servers/save_snapshots.py`. Service layer only; S6 wires routes.

**Deliver.**

1. Store at `PROFILES_DIR/{id}/snapshots/` — `{snapshot_id}.tar.gz` + sidecar `{snapshot_id}.json` manifest. Add `"snapshots"` to `EXCLUDED_TOP_LEVEL` and exclude it from the existing profile-archive builder.
2. `create(session, server, save_uuid, label)` — tar.gz the save point dir; manifest records: snapshot id, label, `created_at`, source uuid, playthrough + save point numbers, `m_sMissionResource`, `m_sGameVersion`, `m_iSavedAtUnix`, `m_iPlaytimeSeconds`, uncompressed size, the server's `scenario_id`, and the full mod set (id, name, pinned version if any) from `ServerMod`.
3. `list_(server_id)`, `rename(id, label)`, `delete(id)`.
4. `restore(session, server, snapshot_id, arm: bool)` — extract back to the original relative path under `.save/game/`; if a save point already occupies it, snapshot the incumbent first, then overwrite. When `arm`, set `save_mode='pinned'` + the restored uuid.
5. `validate_archive(fileobj)` for uploads — regular files and dirs only; no symlinks, hard links or device nodes; no absolute paths; no `..`; member-count cap; **uncompressed** size cap enforced during extraction. Reject whole-archive on first bad member, never partially extract.
6. `store_upload(server, fileobj, label, restore_now, arm)` — validate → store as snapshot → only then optionally call `restore()`.
7. Cap `SAVE_SNAPSHOT_MAX_PER_SERVER` (new setting in `core/config.py`, default 20): at the cap `create` raises → 409. Never auto-delete.
8. Free-space check via `mods/freespace.py` before create and restore. All tar work off the loop via `anyio.to_thread.run_sync`.

**Done when.** PLAN test cases 9, 10, 11, 14, 15 — no more.

**Watch out.**
- Every path through `resolve_safe_path`.
- `restore_now` must not bypass validation — that's test 15.
- Extraction must stream-check size; don't trust the tar header.
- Manifest is the only snapshot metadata store — no DB table.

---

## S6 — saves API router

Create `backend/app/api/server_saves.py`, prefix `/servers/{server_id}/saves`, router-level `dependencies=[Depends(get_current_user)]`. Register in `main.py`.

**Deliver.** Routes — all mutating ones call `_refuse_while_running` **except arming**:

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `""` | save points + snapshots + current selection |
| `POST` | `/{save_uuid}/arm` | `{sticky: bool}` |
| `POST` | `/arm-fresh` | `{sticky: bool}` |
| `DELETE` | `/selection` | back to `latest` |
| `DELETE` | `/{save_uuid}` | delete save point |
| `DELETE` | `/playthroughs/{scenario_dir}/{playthrough_nr}` | delete playthrough |
| `GET` | `/{save_uuid}/download` | tar on the fly |
| `POST` | `/{save_uuid}/snapshot` | `{label}` |
| `PATCH` | `/snapshots/{snapshot_id}` | `{label}` |
| `DELETE` | `/snapshots/{snapshot_id}` | |
| `GET` | `/snapshots/{snapshot_id}/download` | stream from disk |
| `POST` | `/snapshots/{snapshot_id}/restore` | `{arm: bool = true}` |
| `POST` | `/snapshots/upload` | multipart + `{label, restore_now: bool = false, arm: bool = true}` |

Arming a uuid absent from `discover()` → 400. Deleting the armed save point clears the selection.

**Done when.** PLAN test cases 7, 8, 12 — no more. Harness copied from `test_server_files_routes.py`.

**Watch out.** Arming stays allowed while running (the UI offers arm-vs-restart) — everything else 409s. Download uses `Content-Disposition: attachment` like `server_files.py:121-140`.

---

## S7 — MCP tools

**Deliver.** Add `ToolSpec` entries to `backend/app/mcp/tools.py` mirroring S6's routes 1:1, matching the file's existing naming and confirm-flag conventions: `list_server_saves`, `arm_server_save`, `arm_fresh_session`, `clear_save_selection`, `delete_server_save`, `create_save_snapshot`, `restore_save_snapshot`, `delete_save_snapshot`, `rename_save_snapshot`. Skip the binary download/upload routes.

**Done when.** `pytest -q` green (existing MCP tests must still pass). No new tests.

**Watch out.** State-changing tools take the `confirm` flag. Read two neighbouring specs and copy their shape exactly.

---

## S8 — Saves tab

**Deliver.**

1. Types + calls in `lib/api.ts` near `:133-153`, using existing primitives.
2. `"Saves"` in the `tabs` array + one render line in `ServerDetail.tsx`.
3. `components/server/SavesPanel.tsx`, structured after `FilesPanel.tsx`:
   - **Selection banner** — *"Next start: playthrough 0 · save point 12 · <date>"* + Cancel; amber sticky variant reading *"Every restart will load this save point"*. Hidden when mode is `latest`.
   - **Persistence summary** — read-only chips (autosave, retention, load-on-boot) linking to Config.
   - **Save points** — grouped scenario → playthrough, newest first. Columns: #, saved at, playtime, engine version, size. Badges: *"different scenario"*, *"made on 1.8.0.13"*, *"2 mods added, 1 removed"* (names in `title`), *"mod set unknown"*. Actions: Load next start · Snapshot · Download · Delete.
   - **Snapshots** — label, created, source save point, size, mod-drift badge. Actions: Restore · Download · Rename · Delete, plus Upload above the table.
   - **Restore dialog** — summary + overwrite warning + ☑ *"Load this save on next start"* **default on**.
   - **Upload dialog** — file picker + ☐ *"Restore it immediately after upload"* **default off**.
   - **Empty state** — *"This server hasn't written a save yet. Save points appear after the first autosave (every 10 minutes) or a clean stop."*
4. While running: destructive actions disabled + amber banner; **arming stays enabled** and opens a Dialog with *Arm for next restart* / *Restart now and load*.

**Done when.** `npm run build` clean. No tests.

**Watch out.** No toast library — inline `<p className="error">` + query invalidation, as `FilesPanel` does. Labels are generated by us: `m_sSavePointDisplayName` is just a unix timestamp and `m_sPlaythroughDisplayName` is `""`. Never offer "Save now" — no RCON verb exists.

---

## S9 — persistence fields in ServerForm

**Deliver.** A "Persistence" fieldset in `components/server/ServerForm.tsx` with the six config fields (not the three `save_*` selection ones): enabled toggle, autosave interval (0–60, help text "0 disables"), retention (1–128), load-latest-on-boot, keep-saves-after-mission, hive id (0–16383). Wire into the existing submit payload.

**Done when.** `npm run build` clean; values round-trip through the API.

**Watch out.** Mirror the form's existing field/validation idiom. Client ranges must match S2's server-side ones.

---

## S10 — grace audit, integration, RESULTS.md

**Deliver.**

1. Determine `supervisor.stop()`'s grace period and whether the engine's shutdown save completes inside it (SIGKILL likely skips it → lost progress). Report the finding; change the default only if warranted — one line, not a refactor.
2. Full gates: `pytest -q` + `npm run build`.
3. `docker compose up -d --build`, then PLAN's 8-step manual matrix (server 24 has 10 real save points; server 22 has `.save/settings` and no `game/`).
4. `.sprints/10/RESULTS.md` shaped like `.sprints/8/RESULTS.md`: status, story table, data-model changes, new API/FE surface, verification performed, explicit **not exercised** list, observations, and a closing paragraph naming which agent did which story plus any stalls.

**Watch out.** Server 24 was running as of 2026-09-13 — check before any destructive step. Never touch production TrueNAS/SSH.
