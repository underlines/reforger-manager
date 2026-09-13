# Reforger Manager — Sprint 10 plan

Savegame management: browse, arm, snapshot, restore, transfer.

## Context

Arma Reforger has had a first-party **Persistence System** since engine 1.6.0.
Our servers have been quietly writing save points this whole time and the
manager has never shown them. Server 24 (`BADIS | Conflict HQ PvPvE`) currently
holds 10 rotating save points across ~86 minutes of playtime; server 13 holds 9.
The only way to see or touch any of it today is the raw Files tab, which
presents them as anonymous `savepoint007/` directories with no metadata, no
notion of which one the engine will load next, and no protection against
deleting the wrong thing.

`docs/save-state-system-research.md` (uncommitted) started this work. **Two of
its facts were wrong and are corrected below** — both were verified this sprint
against the rendered Bohemia wiki and against our own live servers over the
`armaserver` MCP tools. Trust this document over that one.

Sources:

- [Persistence System](https://community.bistudio.com/wiki/Arma_Reforger:Persistence_System) (1.6.0)
- [Server Config § gameProperties](https://community.bistudio.com/wiki/Arma_Reforger:Server_Config)
- [Startup Parameters § Hosting](https://community.bistudio.com/wiki/Arma_Reforger:Startup_Parameters)
- [Server Management](https://community.bistudio.com/wiki/Arma_Reforger:Server_Management) — the full RCON verb list, which contains no save command
- [tubalainen/reforger-server-manager PR #180](https://github.com/tubalainen/reforger-server-manager/pull/180) — peer project's save-backup feature; prior art for tar-based snapshots, retention caps and archive validation
- [hosthavoc.com — Arma Reforger backups](https://hosthavoc.com/wiki/game-servers/arma-reforger/backups) — commercial panel's restore UX (stop-first, dropdown, zip)

## Guiding principle

**The engine decides what a save *is*; the manager decides which one boots next
and makes sure a good one still exists tomorrow.** We never parse, rewrite or
migrate the engine's save data — a save point is an opaque directory we move as
a unit. Every destructive action is either blocked while the server runs or
takes a snapshot first. The admin should never be able to lose a playthrough by
clicking one button in the wrong order.

## Decisions locked

| # | Decision |
|---|---|
| 1 | Scope is engine-native browse/arm/delete **plus** manager-owned snapshots **plus** download/upload of a snapshot archive. |
| 2 | Emit the full `game.gameProperties.persistence` block from new `Server` columns. Adds a migration. |
| 3 | New **Saves** tab on the server detail page. Persistence *config* fields live in `ServerForm` with the rest of the config; the Saves tab shows them read-only as context. |
| 4 | Load selection is **armed and consumed**: persisted on the server row, applied on the next start, then reset to `latest` — unless the admin explicitly ticks "keep loading this save on every restart". Arming while the server runs opens a dialog offering *Arm for next restart* or *Restart now and load*. |
| 5 | Uploads land in the manager's snapshot store, never directly into the engine's `.save/` tree. Getting an uploaded archive into the game is a second, explicit **Restore** — offered inline on the upload dialog as an opt-in *"restore it now"* checkbox, default **off**. |
| 9 | Mod-drift warnings come from **two** sources: `ServerConfigRevision` history for engine save points (works retroactively on saves already on disk), and the snapshot manifest for snapshots (survives a DB wipe and travels with the archive to another server). Drift is reported as **mod-set** drift — added/removed — never as version drift. |
| 8 | Restore offers *"load this save on next start"* as a checkbox defaulted **on**, rather than arming silently. The engine boots the *newest* save on disk, so a restore without arming appears to do nothing — the safe behaviour must be the default, but it must be visible and refusable. |
| 6 | Snapshot/restore are synchronous requests run off the event loop, not jobs. Payloads are single-digit MB; the jobs system would add latency and UI surface for nothing. |
| 7 | No auto-pruning of snapshots. Past the per-server cap the API refuses with a clear message rather than silently deleting someone's backup. |

## Code-level facts the implementation must respect

1. **The save tree is one level deeper than the old research doc claims.** Ground
   truth from servers 13/22/24 via `list_server_files`:
   `PROFILES_DIR/{id}/profile/.save/game/{GUID}-{mission-name}/playthrough{NNN}/savepoint{NNN}/`.
   Note the `profile/` segment between the server's profile root and `.save`.
   There is **no** `app<appid>_user<userid>` segment (the Overthrow wiki documents
   one; our engine build does not produce it). The old doc's
   `PROFILES_DIR/{id}/.save/...` would find nothing.

2. **`.save/` also contains a `settings/` sibling of `game/`.** Server 22 has
   `.save/settings` and **no** `game/` at all despite having been started and
   stopped — a server with zero saves is a normal state, not an error. Never
   list, snapshot or delete `.save/settings`.

3. **The scenario directory name is not reconstructible from `scenario_id`.**
   Server 24's `scenario_id` is
   `{5F5F49B8F5D935A2}Missions/23_Campaign_HQC_Everon_PvPvE.conf`; its directory
   is `5F5F49B8F5D935A2-23-Campaign-HQC-Everon-PvPvE` — braces stripped, path and
   extension dropped, `_` → `-`. Do **not** derive it. Discover directories by
   globbing, then match a save to the server's configured scenario by comparing
   the parsed `m_sMissionResource` against `Server.scenario_id` exactly.

4. **`meta-info.json` — real contents, server 24 `savepoint012`, verbatim:**

   ```json
   {
       "m_Id": "60b89c6b-4ff3-4d49-9e1e-32966e965333",
       "m_eType": 2,
       "m_iSavedAtUnix": 1789326595,
       "m_iSavePointNr": 12,
       "m_sSavePointDisplayName": "1789326595",
       "m_sMissionResource": "{5F5F49B8F5D935A2}Missions/23_Campaign_HQC_Everon_PvPvE.conf",
       "m_iPlaythroughNr": 0,
       "m_sPlaythroughDisplayName": "",
       "m_iStartedUnix": 1788712484,
       "m_iPlaytimeSeconds": 5163,
       "m_sGameVersion": "1.8.0.13",
       "m_aUsedAddons": []
   }
   ```

   Consequences, all load-bearing:
   - `m_Id` is the UUID `-loadSessionSave` takes. It is the feature's primary key.
   - **`m_aUsedAddons` is empty even though server 24 runs a large mod set.** Do
     not build mod-compatibility warnings on it for engine save points. The
     manager records its own mod set into snapshot manifests instead (F3).
   - `m_sSavePointDisplayName` is the unix timestamp as a string;
     `m_sPlaythroughDisplayName` is `""`. The engine sets no human names — the UI
     generates every label it shows.
   - `m_sGameVersion` **is** populated, so engine-drift warnings are real.
   - Derive ordering from `m_iSavePointNr`/`m_iPlaythroughNr`, not from directory
     names, so an unexpected layout still sorts correctly.

5. **A save point is `meta-info.json` + a `WorldState/` directory** holding a
   single UUID-named `.blob` (2.5 MB on server 24). Treat `WorldState/` as opaque
   and copy it wholesale. Size a save point by summing the files under it.

6. **`game.gameProperties.persistence` — nested inside `gameProperties`, NOT a
   sibling of it.** The old research doc says `game.persistence`; that is wrong
   and would be silently ignored. Verified from the wiki's heading hierarchy,
   where `persistence` (H3) sits under `gameProperties` (H2) alongside
   `missionHeader` (H3), and from the page's own instruction: *"To disable
   persistence entirely, set the enabled save types in
   game->gameProperties->missionHeader to 0"*. Keys, ranges and defaults, quoted:

   | Key | Type | Range | Default | Meaning |
   |---|---|---|---|---|
   | `autoSaveInterval` | number | 0..60 | 10 | Minutes between autosaves; **0 disables** |
   | `saveRetention` | number | 1..128 | 10 | Save points kept for the current mission |
   | `loadSessionSave` | bool | — | `true` | Auto-load latest save point on first startup |
   | `keepSessionSave` | bool | — | `false` | Keep playthrough saves after the mission finishes |
   | `hiveId` | number | 0..16383 | 0 | Separates UUIDs when servers share a persistence DB |
   | `databases` / `storages` | object | — | — | Backend overrides — **out of scope**, see Non-goals |

   `saveRetention`'s default of 10 is confirmed empirically: server 24 holds
   exactly `savepoint003`..`savepoint012`, the older three having rotated out.

7. **Config generation.** `build_config(server, mods) -> dict` at
   `backend/app/servers/config_gen.py:64`, called from `supervisor.start()`
   (`backend/app/servers/supervisor.py:145`). `_deep_merge` (`:111-116`) folds
   `server.extra_config` in **last**, so the admin escape hatch keeps overriding
   whatever we emit — preserve that ordering. `write_config` (`:119-123`) writes
   `CONFIGS_DIR/{id}.json`.

8. **Launch args.** `backend/app/servers/supervisor.py:174-189` builds argv:

   ```python
   args = [
       str(binary),
       "-config", str(config_path),
       "-profile", str(profile_dir),
       "-addonDownloadDir", str(settings.mods_dir / "reforger"),
       "-addonTempDir", str(addon_tmp),
       "-logStats", "30000",
       "-nothrow",
       "-maxFPS", "60",
       "-logLevel", "normal",
   ]
   ```

   The save flags append here. `-profile` already points at
   `settings.profile_dir(server_id)`, which is the root the `profile/.save/` tree
   hangs off — no new path plumbing needed.

9. **Startup flags, quoted from the wiki.** `-loadSessionSave` (1.6.0): *"can be
   used alone to load the latest save, or with a specific save file name…
   Optionally, as a parameter, the UUID of a specific save can be passed, which
   is found inside each save point's meta-info.json file."* `-backendFreshSession`:
   *"skips the initial load request… the DS Session basically starts as a brand
   new one, the rest of functionalities is not affected (saves, runtime loads,
   etc)"* — so a fresh session leaves existing playthroughs on disk and loadable.
   `-keepSessionSave`, `-backendLocalStorage`, `-backendDisableStorage` also
   exist; we only use the first two.

10. **There is no RCON save command.** The authoritative verb list is `#login`,
    `#logout`, `#roles`, `#restart`, `#shutdown`, `#kick`, `#ban create/remove/list`,
    `#id`, `#players`, plus RCON-only `@logout`. Save and load are **boot-time
    only**. The UI must not offer a "Save now" button; the honest equivalent is a
    clean stop, which triggers a save.

11. **Single-server rule and the running guard.** `supervisor.start()`
    (`:116-229`) holds an `asyncio.Lock`, checks `self.is_running()` and queries
    for any other `Server.is_running` row, raising `SingleServerError` → 409.
    `backend/app/api/server_files.py:59-63` has `_refuse_while_running(server_id)`
    returning 409 — reuse this exact helper and its error shape for every
    mutating save route.

12. **Path safety already exists — do not re-derive it.**
    `backend/app/servers/files.py`: `resolve_safe_path(server_id, rel) -> Path`
    (`:112-132`) rejects absolute paths, `..` and NUL bytes textually
    (`_split_segments`, `:76-96`), re-checks ancestry after `resolve(strict=False)`,
    and refuses symlinked components (`_reject_symlink_components`, `:99-109`).
    `EXCLUDED_TOP_LEVEL = frozenset({"logs", "addons_tmp"})` at `:36`. Every save
    path the new code touches goes through `resolve_safe_path`. Note the trap it
    already guards: `Path(base) / "/abs"` discards `base` entirely.

13. **Stop is SIGTERM then SIGKILL after `grace`** (`supervisor.stop()`, `:232-250`).
    The engine writes a save on clean shutdown; a SIGKILL plausibly skips it. The
    grace period is therefore a *data-loss* parameter, not just a tidiness one —
    audit it (F7).

14. **Backup export/import does not touch saves.** `backend/app/api/backup.py:61-98`
    serialises only `Server` config fields, `ServerMod`, `Modpack`/`ModpackItem`.
    This sprint is purely additive there — but the new persistence columns must be
    added to `SERVER_CONFIG_FIELDS` so they survive an export/import round trip.

15. **Frontend tab registration** is a literal const array plus conditional
    renders, `frontend/src/pages/ServerDetail.tsx:14-15, 131-153`:
    `const tabs = ["Config", "Mods", "Files", "Console", "RCON", "History"] as const;`
    Adding `"Saves"` is one array entry and one render line.

16. **There is no toast system.** Panels surface errors inline as
    `<p className="error">{…}</p>` with local `setActionError` state; success is
    implicit via query invalidation. Follow that — do not add a toast library.
    `FilesPanel.tsx` is the structural model: `useQuery` fetch (`:52-55`),
    loading/error/empty ladder (`:308-417`), running-guard banner (`:222-226`),
    click-to-arm-then-confirm delete with a 4 s auto-clear (`:48, 127-131, 379-409`),
    `Dialog` from `ui.tsx` for modals (`:419-475`).

17. **API primitives already cover every shape needed** — `frontend/src/lib/api.ts`:
    `api<T>()` (`:168-179`), `apiVoid()` (`:182-192`), `apiUpload<T>()` (`:195-209`),
    `apiBlob()` (`:212-222`), `saveBlob()` (`:225-234`). No new wrapper. Types go
    beside the `FileEntry`/`DirListing` block (`:133-153`).

18. **Tests have no `conftest.py`.** Each file is a self-contained
    `unittest.IsolatedAsyncioTestCase` that builds its own `FastAPI()`, overrides
    `get_session`/`get_current_user`, uses `sqlite+aiosqlite:///:memory:`, and
    patches `settings.profiles_dir` at a tmpdir. Copy
    `backend/tests/test_server_files_routes.py:33-64` verbatim as the harness.

19. **`mods/freespace.py` already implements a free-space guard** — reuse it
    before snapshot and restore rather than writing a new one.

20. **Config-revision history is already a usable "what ran when" log.**
    `ServerConfigRevision` (`backend/app/models/server.py:115-137`) stores the
    **entire** generated config in `snapshot` (a `JSONVariant`, not a diff or a
    hash) with a timezone-aware `created_at`. Its sole write site is
    `supervisor.start()` (`backend/app/servers/supervisor.py:144-159`), which
    inserts a row **unconditionally on every start** — config edits via the API
    do *not* create revisions. That is the correct granularity here, because mods
    only take effect at start. **Nothing prunes the table** — no cap, no prune
    job; server 24 retains all 6 revisions back to 2026-09-06. So the mod set in
    effect at a save's `m_iSavedAtUnix` is the `game.mods` array of the newest
    revision whose `created_at` is at or before that instant.

    No new endpoint is needed for this: the saves listing already runs with an
    `AsyncSession`, so it queries `server_config_revisions` directly.

21. **Mod entries carry guid and name, but `version` only when pinned.**
    `config_gen.py:89` emits `"mods": [m.to_dict() for m in mods]`; `ModEntry.to_dict()`
    (`:55-61`) adds `version` only if `ModEntry.version` is set, which comes from
    `ServerMod.pinned_version`. Server 24 pins none of its 11 mods, so its stored
    revisions record `{"modId": "...", "name": "..."}` with no version. Therefore
    drift detection compares **mod-id sets**, and the UI says "added/removed",
    never "updated". **Do not "fix" this by always stamping a resolved version:**
    in Reforger's config a `version` on a mod entry *pins* that mod, so emitting
    one unconditionally would silently pin every mod on every server. See
    Non-goals.

22. **`ServerMod` is current-state only** (`backend/app/models/server.py:90-112`) —
    `pinned_version`/`pinned_at`/`pinned_reason` describe the present pin, not a
    history. There is no audit log and no other timestamped record of a past mod
    set. Config revisions are the only source; do not go looking for another.

## Feature set

### F1 — Save discovery (`backend/app/servers/saves.py`)

New module. No DB reads beyond the `Server` row.

- `discover(server_id, scenario_id) -> ScenarioSaves[]`. Glob
  `profile/.save/game/*/playthrough*/savepoint*/meta-info.json` under the
  server's profile root, **plus** a bounded fallback glob for `meta-info.json`
  at depth ≤ 6 under `.save/`, so an `app*_user*`-style layout (documented
  upstream, absent here) is still found. Deduplicate by resolved path.
- Parse each `meta-info.json` defensively. A file that is missing, unreadable or
  not valid JSON yields a record with `readable: false` and whatever the
  directory names imply — it must appear in the listing so the admin can delete
  it, never silently vanish.
- Group by scenario directory → playthrough (`m_iPlaythroughNr`) → save point
  (`m_iSavePointNr`), each sorted descending so the newest is first.
- Per save point compute: `uuid` (`m_Id`), `saved_at` (from `m_iSavedAtUnix`),
  `playtime_seconds`, `game_version`, `size_bytes` (recursive sum),
  `matches_current_scenario` (`m_sMissionResource == server.scenario_id`),
  `engine_drift` (`m_sGameVersion` vs the installed engine build).
- Skip `.save/settings` and any non-`savepoint*` directory.

**Mod drift for engine save points (F-facts 20–21).** In the same query pass,
load the server's `ServerConfigRevision` rows (`server_id`, ordered by
`created_at`). For each save point, pick the newest revision with
`created_at <= saved_at`, read `snapshot["game"]["mods"]`, and diff its `modId`
set against the server's current `ServerMod` set. Emit
`mod_drift: {added: [names], removed: [names]} | null`, plus
`mod_drift_unknown: true` when no revision precedes the save (a wiped database,
or a save older than any recorded start) — an unknown must read as unknown in
the UI, never as "no drift". Compare **ids**, display **names**; never claim a
version changed.

### F2 — Load selection: arm and consume

Three new `Server` columns — `save_mode` (`latest` | `pinned` | `fresh`, default
`latest`), `save_pinned_uuid` (nullable str), `save_selection_sticky` (bool,
default `false`).

- `supervisor.start()` translates them into argv (F-fact 8): `latest` appends
  **nothing** (preserving today's behaviour exactly); `pinned` appends
  `-loadSessionSave <uuid>`; `fresh` appends `-backendFreshSession`.
- After the process spawns successfully and in the same transaction that sets
  `is_running`, if `save_selection_sticky` is false and the mode is not `latest`,
  reset to `latest` with a null uuid. This is the whole safety argument for the
  design: a permanently pinned UUID would make every scheduled nightly restart
  silently rewind the server and discard the session.
- Arming a uuid that no longer exists on disk → the API refuses at arm time; if
  it vanishes between arming and starting, `start()` logs a warning, falls back
  to `latest` and clears the selection rather than failing the start.

### F3 — Manager snapshots

Retention-proof copies that survive the engine's `saveRetention` rotation.

- Stored at `PROFILES_DIR/{id}/snapshots/` as `{snapshot_id}.tar.gz` plus a
  sidecar `{snapshot_id}.json` manifest. Add `"snapshots"` to
  `EXCLUDED_TOP_LEVEL` (F-fact 12) so the Files tab cannot mangle them, and
  exclude the directory from the existing profile-archive builder so profile
  zips don't double-count.
- Built with Python `tarfile` (peer-project precedent), written off the event
  loop via `anyio.to_thread.run_sync`.
- **The manifest is the portable half of the mod-drift story** (F1 covers engine
  save points from revision history; this covers snapshots). It is not redundant
  with F1: a snapshot downloaded and uploaded to a *different* server has no
  relevant revision history there, and a manifest survives a database wipe
  because it travels inside the archive. Record:
  snapshot id, admin label, `created_at`, source save uuid, playthrough and save
  point numbers, `m_sMissionResource`, `m_sGameVersion`, `m_iSavedAtUnix`,
  `m_iPlaytimeSeconds`, uncompressed size, the server's `scenario_id` at capture
  time, and the **full mod set** (mod id, name, and pinned version where one
  exists) taken from `ServerMod`. Restore diffs that recorded set against the
  server's current one and warns concretely — mods drifting out of sync is the
  documented leading cause of saves failing to load. Same rule as F1: compare
  ids, display names, never claim a version changed (F-fact 21).
- Cap at `SAVE_SNAPSHOT_MAX_PER_SERVER` (new setting, default 20). At the cap,
  creation returns 409 naming the cap; nothing is auto-deleted.
- Free-space check via `mods/freespace.py` before writing.

### F4 — Restore, delete, download, upload

- **Restore** (409 while running): extract the archive back to its original
  relative path under `.save/game/`. If a save point already occupies that path,
  snapshot the incumbent first, then overwrite.

  The request body carries `arm: bool`, and the restore dialog presents it as
  *"Load this save on next start"* **ticked by default**. Rationale: the engine
  boots the newest save point on disk, which after restoring an older one is not
  the restored one — so a restore without arming silently appears to have done
  nothing, and reads as a bug. Defaulting the checkbox on closes that footgun;
  keeping it a checkbox rather than a hidden side effect means one button never
  quietly changes two things. Unticking it is the "just stage the file for
  later" path.
- **Delete** (409 while running): a single save point, or a whole playthrough
  directory. Both refuse to touch `.save/settings`. Deleting the save point
  currently armed also clears the selection.
- **Download**: snapshots stream from disk; an engine save point is tarred on the
  fly. `Content-Disposition: attachment`, modelled on
  `server_files.py:121-140`.
- **Upload**: multipart `.tar.gz` → validated → stored as a snapshot, never
  written into `.save/` directly (Decision 5). Validation, mirroring the peer
  project: regular files and directories only; no symlinks, hard links or device
  nodes; no absolute paths; no `..`; a member-count cap; and an **uncompressed**
  size cap checked while extracting, to stop a decompression bomb. Reject the
  whole archive on the first bad member — never partially extract.

  The upload form carries a `restore_now: bool`, default **off**. When set, the
  endpoint performs the ordinary restore path (including its `arm` default)
  *after* validation and storage succeed — so the quarantine step still happens
  in full and a rejected archive can never reach `.save/`. This is a convenience
  wrapper over two existing calls, not a second code path: implement it by
  calling the restore service function, not by duplicating extraction logic.

### F5 — Persistence config (`game.gameProperties.persistence`)

Six new `Server` columns: `persistence_enabled` (bool, default `true`),
`auto_save_interval` (0–60, default 10), `save_retention` (1–128, default 10),
`load_session_save` (bool, default `true`), `keep_session_save` (bool, default
`false`), `hive_id` (0–16383, default 0). Validated at the Pydantic schema layer
against the wiki ranges in F-fact 6.

`build_config` emits `game.gameProperties.persistence` with all six values
(matching engine defaults, so the emitted config is behaviourally a no-op for
existing servers). When `persistence_enabled` is false, emit
`game.gameProperties.missionHeader.m_eSaveTypes = 0` instead of the block.
`extra_config` still merges last. Add all six to `SERVER_CONFIG_FIELDS` in
`backend/app/api/backup.py` (F-fact 14).

### F6 — Saves tab (`frontend/src/components/server/SavesPanel.tsx`)

Register `"Saves"` in `ServerDetail.tsx`. Panel layout, top to bottom:

1. **Selection banner** — *"Next start: playthrough 0 · save point 12 ·
   2026-09-13 08:29"* with a Cancel button, or the sticky variant in amber
   reading *"Every restart will load this save point"*. Absent when mode is
   `latest`.
2. **Persistence summary** — read-only chips (autosave 10 min, retention 10,
   load latest on boot) with a link to the Config tab. Editing happens in
   `ServerForm`, not here.
3. **Save points** — grouped scenario → playthrough, newest first. Columns: save
   point #, saved at, playtime, engine version, size. Badges for
   `!matches_current_scenario` (*"different scenario"*), `engine_drift`
   (*"made on 1.8.0.13"*) and `mod_drift` (*"2 mods added, 1 removed"*, the
   names in a tooltip; *"mod set unknown"* when `mod_drift_unknown`). Row
   actions: **Load next start**, **Snapshot**, **Download**, **Delete**.
4. **Snapshots** — label, created, source save point, size, and a mod-drift badge
   from the manifest diff. Actions: **Restore**, **Download**, **Rename**,
   **Delete**, plus an **Upload** button above the table.

The two dialogs that carry a defaulted checkbox, spelled out so they are built
consistently:

- **Restore** — summarises the snapshot (label, captured, source save point, mod
  drift if any), warns when it will overwrite an existing save point and that the
  incumbent is snapshotted first, and offers ☑ *"Load this save on next start"*
  (default **on**, Decision 8).
- **Upload** — file picker plus ☐ *"Restore it immediately after upload"*
  (default **off**, Decision 5). When ticked, the restore's own arm checkbox
  applies with its default, and the dialog says so in one line rather than
  nesting a second checkbox.

Empty states carry real guidance: no `.save/game` at all → *"This server hasn't
written a save yet. Save points appear after the first autosave (every 10
minutes) or a clean stop."* — the exact situation server 22 is in today.

Guard behaviour while running: destructive actions are disabled with the standard
amber banner. **Arming stays enabled** and opens a `Dialog` offering *Arm for
next restart* (writes the selection, leaves the server alone) and *Restart now
and load* (writes it, then stop → start). No silent no-op.

### F7 — Shutdown-grace audit

Establish what `supervisor.stop()`'s grace period actually is and whether the
engine finishes its shutdown save inside it. If the margin is thin, lift it to a
setting with a safer default and say so in the stop confirmation. Deliverable is
a finding plus, only if warranted, a one-line default change — not a refactor.

## Data-model changes

One Alembic revision (`0005_server_persistence_and_save_selection`), all columns
on `servers`, all with server-side defaults so existing rows migrate cleanly:

| Column | Type | Default |
|---|---|---|
| `persistence_enabled` | bool | `true` |
| `auto_save_interval` | int | `10` |
| `save_retention` | int | `10` |
| `load_session_save` | bool | `true` |
| `keep_session_save` | bool | `false` |
| `hive_id` | int | `0` |
| `save_mode` | str | `'latest'` |
| `save_pinned_uuid` | str, nullable | `NULL` |
| `save_selection_sticky` | bool | `false` |

No new tables: snapshot metadata lives in on-disk manifests beside the archives,
so a snapshot is self-describing and survives a database wipe.

## Testing

Backend (`backend/tests/`, harness copied from `test_server_files_routes.py`):

1. Discovery against a seeded fake tree — two playthroughs, several save points,
   correct grouping and descending order.
2. Discovery tolerates a corrupt/missing `meta-info.json` (record present,
   `readable: false`).
3. `.save/settings` and non-`savepoint*` directories are never listed.
4. `matches_current_scenario` true/false against `m_sMissionResource`.
5. `build_config` emits `game.gameProperties.persistence` at the **exact** nesting
   with all six values; `persistence_enabled=false` emits `m_eSaveTypes: 0`
   instead; `extra_config` still wins the merge.
6. Arm → start consumes the selection back to `latest`; sticky arm does not.
7. Arming an unknown uuid → 400.
8. Every mutating route returns 409 while that server runs; arming does not.
9. Snapshot round trip: create → restore → the save point is byte-identical.
10. Snapshot cap returns 409 and deletes nothing.
11. Upload validation rejects, each as its own case: absolute member path, `..`
    traversal, symlink member, oversize uncompressed payload.
12. Deleting the armed save point clears the selection.
13. Mod drift: a save whose preceding revision listed mods {A,B} against a
    current set of {B,C} reports `added: [C], removed: [A]`; an identical set
    reports `null`; a save with no preceding revision reports
    `mod_drift_unknown: true` (and **not** an empty diff).
14. `restore(arm=True)` sets the selection to the restored uuid;
    `restore(arm=False)` leaves it untouched.
15. `upload(restore_now=True)` on an archive that fails validation writes nothing
    and restores nothing — the rejection wins over the convenience flag.

Cover exactly these. Frontend is untested; `npm run build` is the only gate.

Manual matrix on a rebuilt stack (server 24 has real saves; server 22 has none):

1. Saves tab lists server 24's 10 save points with correct timestamps and sizes.
2. Server 22 shows the no-saves empty state, not an error.
3. Arm a save point while stopped → banner appears → start → engine log shows the
   save loading → banner clears.
4. Arm while running → dialog → *Arm for next restart* leaves the server up.
5. Snapshot → delete the engine save point → restore → it reappears.
6. Download a snapshot, upload it back, restore it.
7. Persistence fields round-trip through `ServerForm` into the generated config.
8. Server 24's older save points show a mod-drift badge consistent with its 6
   recorded revisions; its newest save point (written inside the current
   revision's run) shows none.

## Cut from this sprint / Non-goals

- **A "Save now" button.** No RCON verb exists (F-fact 10). Offering one would be
  a lie. A clean stop is the honest equivalent and we say so in the UI.
- **`databases` / `storages` persistence overrides.** Advanced, backend-swapping,
  no demand; `extra_config` remains the escape hatch.
- **Always stamping a resolved mod `version` into the generated config.** It
  would upgrade drift detection from mod-set to version-level, and it is
  tempting, and it is dangerous: in Reforger's config a `version` on a mod entry
  *pins* that mod, so emitting one unconditionally would silently pin every mod
  on every server — changing update behaviour fleet-wide as a side effect of a
  savegame feature. If version-level drift is ever wanted, it needs its own
  sprint and its own field, not a widened `ModEntry.to_dict()` (F-fact 21).
- **A public config-revision history API.** F1 reads
  `server_config_revisions` directly through the session it already holds.
  Exposing revisions as a browsable endpoint is a reasonable feature on its own
  merits — it is just not this sprint's.
- **Scheduled/automatic snapshots** (e.g. nightly, or before an engine update).
  The natural next sprint once the primitives exist and `NIGHTLY_CHECK_ENABLED`
  can host it.
- **Cross-server restore in the UI.** Download-then-upload covers it; a server
  picker adds scenario/mod-compatibility questions worth their own design.
- **Reading or editing `WorldState` blobs.** Opaque, by design.
- **Whole-`profile/` backups.** The peer project treats all of `profile/` as save
  data; our existing `GET /files/archive` (`server_files.py:143-152`) already zips
  the profile directory, so that need is met.
- **Production TrueNAS / SSH.** Out of scope, as always.

## Verification

```bash
cd backend && .venv/Scripts/python.exe -m pytest -q
cd frontend && npm run build
docker compose up -d --build   # then the manual matrix above at :18090
```
