# Reforger Manager — Sprint 2: close the UI ↔ API gap, finish the deferred features

> Sprint 1 (`.sprints/1/`) built and deployed the stack: FastAPI backend, Postgres,
> process supervisor, mod subsystem, pre-flight, engine tracking, and a React SPA — live at
> `https://armaserver.badis.net`, cutover from `armaservermanager` complete.
> Implementation phases for this sprint live in [STORIES.md](STORIES.md).

## Context

Sprint 1 shipped a working backend whose surface the frontend only half-uses. The API already
supports creating, editing and deleting server definitions, managing a definition's mod set and
load order, per-server version pins, adding mods by URL/ID, Workshop search, rich mod detail
(versions, dependency tree, "used by"), and free-form RCON including `#kick` / `#ban`. **None of
that has a UI.** The `servers` page is a read-only list, the server-detail *Config* and *Mods*
tabs are read-only, and there is no "new server" control anywhere. Start/stop *is* wired.

Separately, several PLAN features were deferred in Sprint 1 and have no code at all: modpacks
(tables exist, zero routes, stub page), backup/restore, storage/orphan management, RCON kick/ban +
scheduled restart, and runtime-mutable settings (password change, nightly-check toggle — both
env-only today).

**Sprint 2 goal:** every useful backend capability is reachable from the web UI, and every
remaining PLAN feature is built. After this sprint the SPA is the complete operator surface;
SSH-ing the NAS is never required for normal operation.

### Guiding principle

*If an API capability is useful to an operator, it has a UI. If a PLAN feature has a data model
but no routes, it gets routes and a UI.* The only deliberate exceptions are listed under
**Non-goals** at the end.

## Decisions locked for this sprint

| Decision | Choice |
|---|---|
| Sprint width | **All remaining PLAN features** — nothing deferred to Sprint 3 except the explicit Non-goals. |
| Config revisions | **Cut.** No diff, no rollback, no revisions UI. The experiment workflow is **clone** (§A6): duplicate, edit the copy, delete it if bad. See *Cut from this sprint*. |
| Server-config editing | **Hybrid**: structured form for common fields + an *Advanced* collapsible with a raw-JSON editor for `game_properties` extras and `extra_config`. |
| Editing a running definition | **Allowed.** Config is only read at process start, so an edit cannot disturb a live server; the form shows a *"changes apply on next start"* banner. Delete and modpack-apply stay refused while running. |
| Modpacks depth | **Full**: CRUD, drag-order, apply-to-server, create-pack-from-server, JSON import/export. |
| Scenario selection | **Picker + free-text override**: dropdown populated from the definition's selected mods' scenarios, plus a free-text field to paste a `{GUID}Missions/x.conf` directly (offline-scanned gameIds are not always authoritative — see `docs/REF.md`). |
| Frontend stack | React 19 + Vite + TanStack Query + Tailwind + the hand-rolled `components/ui.tsx`. Add `@dnd-kit/core` + `@dnd-kit/sortable` for load-order drag. No other new runtime deps. |
| Frontend formatting | **Reformat the existing frontend first** (§G32) — conventional formatting, one component per file, before feature work lands on top. |
| Auth model | Still a single admin account. Add **self-service password change** only. Multi-user / roles is a Non-goal. |

## Code-level facts the stories must respect

Verified against the Sprint 1 tree on 2026-09-02. Each of these invalidates an obvious-looking
implementation, so they are recorded here rather than rediscovered per story.

1. **`PATCH /api/servers/{id}` replaces the mod set destructively.** `_apply_mods`
   (`api/servers.py:75`) calls `server.mods.clear()` and rebuilds every `ServerMod` row from the
   payload. Any pin (`pinned_version` / `pinned_at_build` / `pinned_reason`) not present in the
   request body is **silently lost**, and `pinned_at` is dropped unconditionally because
   `ServerModIn` has no such field. Both the mod-set save (§A5) and modpack apply (§C16) go
   through this path.
2. **`GET /api/servers/{id}/config` regenerates from the persisted row.** It cannot preview
   unsaved form state, which §A2 requires. Hence the new preview route.
3. **The spam filter lives in the backend.** `mods/logview.py:43` holds a module-level
   `_SPAM_PATTERNS` tuple; `is_spam_line` is what filters both the console WebSocket
   (`api/servers.py:333`) and `GET /api/servers/{id}/log`. The frontend's `isSpam()`
   (`ServerDetail.tsx:100`) is a second, cosmetic copy. Making patterns configurable means
   changing the backend one.
4. **`NightlyCheckScheduler` is a local variable inside `lifespan`** (`main.py:206`), unreachable
   from any route. Toggling it at runtime (§E25) needs it promoted to a module-level singleton.
5. **The admin user is seeded only when the users table is empty** (`main.py:94`), so a
   self-service password change (§E26) survives restarts and does not fight `ADMIN_PASSWORD`.
6. **The `mod_download` job already takes `{guids: [...], versions: {guid: version}}`**
   (`main.py:155`) and is registered. §B11 is a route over an existing job, not new job code.
7. **The Workshop client is rate-limited** (60/min, burst 20) with a TTL cache
   (`mods/workshop.py`). Anything that fans out per-mod (§A4 scenarios) must read the DB first.
8. **`server_mod_entries` drops disabled mods** (`config_gen.py:126`) and `build_config` merges
   `game_properties` over `DEFAULT_GAME_PROPERTIES` and deep-merges `extra_config` last. The
   generated config is therefore a lossy projection of the definition — which is the reason
   config-revision rollback was cut.

---

## Feature set

### A. Server definitions in the UI

1. **Create** — a "New definition" button on `/servers` opens the same form used for editing,
   pre-filled with the schema defaults (`ServerBase`: ports 2001 / 17777 / 19999, `max_players`
   32, RCON enabled, `visible` true). `POST /api/servers`.
2. **Edit** — the *Config* tab becomes an editable form (currently a read-only `<dl>`):
   - **Common fields**: name, `game_name`, `scenario_game_id` (via the picker, §A4),
     `game_password`, `admin_password`, `max_players`, `visible`, `is_favourite`,
     RCON on/off + `rcon_port` + `rcon_password` + `rcon_permission`, and the known
     `game_properties` keys seen in the migrated configs (`serverMaxViewDistance`,
     `serverMinGrassDistance`, `networkViewDistance`, `disableThirdPerson`, `fastValidation`,
     `battlEye`, `VONDisableUI`, `VONDisableDirectSpeechUI`).
   - **Advanced (collapsed by default)**: `bind_address` / `bind_port` / `public_address` /
     `public_port` / `a2s_address` / `a2s_port` / `rcon_address` / `rcon_max_clients`, a
     raw-JSON editor for any `game_properties` key not in the form, and a raw-JSON editor for
     `extra_config`. Both JSON editors validate before the form will save.
   - Saves via `PATCH /api/servers/{id}` (already PATCH-semantics).
   - **Live preview** of the generated `config.json` from the *unsaved* form state via the new
     `POST /api/servers/{id}/config/preview`, which takes a `ServerUpdate` body, applies it to a
     detached copy of the row and returns `build_config` output without persisting. (Fact #2 —
     the existing GET cannot do this, and re-implementing `build_config` in TypeScript would
     fork the config generator.)
   - Editing is **allowed while the definition is running**; the form shows a *"this server is
     running — changes apply on next start"* banner. `build_config` output is only consumed by
     `supervisor.start`, so a live process is unaffected.
3. **Delete** — a confirm dialog on the server-detail page. `DELETE /api/servers/{id}`
   (already 409s while running).
4. **Scenario picker** — new endpoint `POST /api/scenarios/resolve` `{guids: [...]}` returns
   `[{game_id, name, game_mode, player_count, mod_guid, mod_name, source: "db"|"api"}]` for the
   given mod set. **DB-first**: read `mod_scenarios` for every GUID and call
   `workshop.get_scenarios` only for GUIDs with no cached rows, persisting what comes back
   (fact #7 — a 20-mod definition must not become 20 Workshop calls on every form render).
   A GUID that 404s or times out is reported as such rather than failing the whole request.
   The Config form calls it with the definition's current mods; the result feeds a `<select>`,
   with a "paste a scenario id" free-text field beside it that always wins if non-empty.
5. **Mod set management** (the *Mods* tab, currently read-only):
   - Add mods from the library via a searchable multi-select (filtered to `is_local` by
     default, toggle to include not-local), showing size and the dependency additions each
     brings (`GET /api/mods/{guid}` → `dependency_tree`).
   - Remove a mod from the set.
   - **Drag-and-drop load order** (`@dnd-kit/sortable`) — Reforger load order is significant.
   - Per-mod enable/disable toggle (`ServerMod.enabled`).
   - All of the above mutate a local working copy; a "Save mod set" button issues one
     `PATCH /api/servers/{id}` with the full `mods[]` (the existing replace-semantics transport
     — atomic, no new routes needed).
   - **Pin preservation is mandatory** (fact #1). Two changes, both required:
     `_apply_mods` carries the existing `pinned_*` columns forward for any `mod_guid` that
     survives the replace unless the payload explicitly sets them; and the UI round-trips the
     pin fields it received. Belt and braces — a pin silently vanishing on an unrelated
     reorder is the worst failure this tab can have.
   - **Per-server version pin / unpin** inline on each assigned mod
     (`POST` / `DELETE /api/servers/{id}/mods/{guid}/pin` — already exist, no UI today).
   - The pre-flight panel already here stays; it re-runs after a mod-set save.
6. **Clone** — "Duplicate this definition" on server-detail. New endpoint
   `POST /api/servers/{id}/clone` `{name}` deep-copies the row + its `server_mods` (including
   pins; **not** runtime state, **not** `config` / `config_revision` / `config_revisions`).
   **Clone is the experiment workflow** — duplicate, edit the copy, delete it if it turns out
   bad; the original is never modified. This is what replaces config-revision rollback.
7. **Favourite** — a star toggle on the `/servers` list rows and the detail header
   (`is_favourite`, already in the schema, no UI). Favourites sort first.

### B. Mod library completion

8. **Add a mod** — "Add mod" control on `/mods` accepting a Workshop URL or a bare 16-hex
   GUID. `POST /api/mods/add` (exists). On success the row appears with API enrichment.
9. **Workshop search + add** — inline search box (`GET /api/mods/search?q=`) showing
   name / summary / latest version / workshop link, each with an "Add" button that funnels
   into `POST /api/mods/add`.
10. **Mod detail view** — clicking a library row opens a detail page/drawer backed by
    `GET /api/mods/{guid}`: full summary, tag list, the **version history** table
    (`versions[]` with `gameVersion` per version), the **resolved dependency tree**
    (`dependency_tree`, with `via: api|gproj|unknown` and `state` badges), and
    **"used by"** (which server definitions reference it). Pin/unpin and verify live here too.
11. **Force (re)download** — new route `POST /api/mods/{guid}/download` `{version?}` enqueues
    the already-registered `mod_download` job (fact #6); a "Re-download" button on the mod row /
    detail. The Jobs page already streams its progress.
12. **Storage & orphans** — new route `GET /api/storage` →
    `{mods_path, free_bytes, total_bytes, per_mod: [{guid, name, bytes}], orphans: [...],
    kept_as_dependency: [{guid, required_by: [...]}]}`.
    **An orphan is `is_local` AND absent from `server_mods` AND absent from `modpack_items` AND
    absent from the resolved dependency closure of both.** The closure term is not optional: a
    dependency-only mod (RHS bases, CBA-likes) appears in no `server_mods` row, so the naive
    definition would offer to delete files a running definition needs. Mods excluded solely by
    the closure are reported separately as `kept_as_dependency` with the parents that hold them,
    so the operator can see *why* something is not deletable.
    New page/section shows the free-space bar, the per-mod size table (sorted desc), the orphan
    list with a **"Remove from disk"** action (`DELETE /api/mods/{guid}/local` → deletes the
    addon dir, sets `is_local = false`, keeps the DB row so it can be re-added), and the
    kept-as-dependency list as read-only context. `DELETE …/local` re-checks the orphan
    condition server-side and 409s if the mod became referenced, and refuses outright while any
    server is running.
13. **Free-space guard** — `POST /api/mods/{guid}/download`, `/api/mods/updates/apply` and
    `/api/servers/{id}/mods/update/apply` compare projected download size (sum of `Mod.size`
    for the affected GUIDs, treated as an estimate) against free space and return `409` with a
    clear message when it would not fit; the UI surfaces it before enqueuing.

### C. Modpacks — end to end

New router `app/api/modpacks.py`. Model already exists (`modpacks`, `modpack_items`).

14. **CRUD** — `GET /api/modpacks`, `POST /api/modpacks` `{name, description, items:[{mod_guid,
    load_order}]}`, `GET /api/modpacks/{id}`, `PATCH /api/modpacks/{id}`,
    `DELETE /api/modpacks/{id}`.
15. **Drag-order items** — `@dnd-kit`, same component as §A5, persisted via `PATCH`.
16. **Apply to a server** — `POST /api/modpacks/{id}/apply/{server_id}` `{mode: "replace"|
    "append"}` writes the pack's items into `server_mods` (replace clears first). Refuses while
    that server runs. **Pins on GUIDs that survive the apply are preserved** (fact #1) —
    `replace` on a pack that still contains a pinned mod must not silently unpin it; the
    response reports which pins were dropped because their mod left the set.
17. **Create pack from a server** — `POST /api/modpacks/from-server/{server_id}` `{name,
    description}` snapshots the definition's current mod set + order into a new pack. Pins are
    **not** carried into the pack (a pack is a mod list, not a version lock) — the response
    says so when the source had pins.
18. **JSON import / export** — `GET /api/modpacks/{id}/export` → a portable JSON document
    (`{name, description, items:[{mod_guid, load_order}]}`); `POST /api/modpacks/import`
    `{...that shape...}` with name-collision handling (`?on_conflict=rename|replace|error`).
19. **UI** — the stub `/modpacks` page becomes: a pack list, a create/edit form with the
    drag-order mod multi-select (reusing §A5), per-pack **Apply to…**, **Export**, **Delete**,
    a global **Import** button, and **"Save current mod set as a pack"** on server-detail.

### D. RCON player management & scheduled restart

The RCON client already parses `#players` into structured rows and already whitelists
`#kick <n>` / `#ban <n>` (`rcon/client.py:236`).

20. **Player list** — the *Players* tab renders `GET /api/servers/{id}/players`
    (`parse_players` output) as a table: name, id, IP, ping. Auto-refresh while the tab is open.
21. **Kick / ban** — per-row buttons issuing `POST /api/servers/{id}/rcon` `{command: "#kick N"}`
    / `"#ban N"` with a confirm dialog. (Endpoint + client already support these.)
22. **Broadcast** — keep the existing `#say` box; keep `#restart` / `#shutdown` with confirms.
23. **Scheduled restart with warnings** — `POST /api/servers/{id}/schedule-restart`
    `{in_seconds, warn_at: [300, 60, 10]}`, `GET /api/servers/{id}/schedule-restart` (so the UI
    countdown survives a page reload) and `DELETE /api/servers/{id}/schedule-restart`.
    The supervisor gains one cancellable timer (single running server ⇒ one schedule): it
    sends `#say` warnings at each `warn_at` offset, then `#restart`. The *Players* tab shows a
    countdown + cancel when a schedule is armed.
    **The schedule is in-memory only** — it is cancelled when the server stops, when the
    definition is stopped and restarted, and it does not survive a backend restart. The GET
    response says `armed: false` in those cases rather than showing a stale countdown. Warnings
    that fail to send (RCON down) are logged and do not abort the restart.

### E. Settings write-back

24. **`app_settings` singleton table** (`id = 1`) for runtime-mutable settings:
    `nightly_check_enabled` (bool), `nightly_check_hour` (int), `log_spam_patterns` (string[],
    seeded from today's hard-coded `thermalProfileDefault.conf`). Read at startup (falls back to
    env / current defaults when the row is absent), exposed as `GET /api/settings` /
    `PATCH /api/settings`.
25. **Nightly-check toggle at runtime** — `PATCH /api/settings` starts or stops the
    `NightlyCheckScheduler`. This requires promoting it from a `lifespan` local to a module-level
    singleton with `start()` / `stop()` reachable from the route (fact #4); `lifespan` then owns
    only its initial state and its shutdown. Settings page gets the toggle + hour field.
26. **Change password** — `POST /api/auth/password` `{current_password, new_password}`
    (verifies `current`, re-hashes, updates the row, keeps the session). Settings page form.
    Safe to persist: the bootstrap seed is empty-table-only (fact #5).
27. **Log spam patterns** — `logview.is_spam_line` reads the settings-backed pattern list
    instead of its module constant, cached in-process and invalidated on `PATCH /api/settings`
    (fact #3). This is the change that actually matters: `is_spam_line` filters the console
    WebSocket and the stored-log endpoint. The frontend's duplicate `isSpam()` is deleted and
    the Console tab trusts the backend's `is_spam` flag. Settings page lets the operator edit
    the list.
28. The Settings page's "backend-not-yet-exposed" notice is removed once the above land.

### F. Backup / restore

29. **Export** — `GET /api/backup/export` → one JSON document containing every server
    definition (fields + `server_mods` incl. pins) and every modpack. Excludes runtime state,
    the engine row, and the disk-derived mod library (all reconstructable).
30. **Import** — `POST /api/backup/import?dry_run=true` returns a plan (what would be created /
    replaced / skipped, by name); `?dry_run=false` applies it. Definitions and packs are keyed
    by name; `?on_conflict=skip|replace` (default `skip`). Never touches a running server.
31. **UI** — Settings page: "Download backup" and "Restore from file" (with the dry-run
    preview shown before the operator confirms).

### G. Hardening & ops

32. **Frontend reformat** — before feature work: reformat every file under `frontend/src/` to
    conventional multi-line formatting and split the multi-component files (`ServerDetail.tsx`
    holds five panels, `Settings.tsx` four cards) into one component per file under
    `src/components/` and `src/pages/`. Behaviour-neutral; the diff is large by design and is
    reviewed as a single mechanical change before anything is built on top of it.
33. **CORS** — set `CORS_ORIGINS` in `.env` to the real origins
    (`https://armaserver.badis.net`, `http://192.168.1.10:18090` for LAN-direct,
    `http://localhost:5173` for dev) and drop the `["*"]` default from `core/config.py`.
    `allow_credentials` stays true (now valid with an explicit list). Note the SPA is served
    same-origin by FastAPI, so this only affects the dev server and any stray cross-origin
    caller — it is hygiene, not a live vulnerability.
34. **Old-stack leftover cleanup** (NAS, gated per `Z:\CLAUDE.md`) — archive
    `/mnt/Apps/docker/armaservermanager/steam/logs` to
    `Z:\_archive\armaservermanager-steam-logs-2026-09-02.tar.zst`, then `rm -rf`
    `/mnt/Apps/docker/armaservermanager/` (the `steam/`, `steamcontent/` runtime remnants —
    the compose dir under `Z:\armaservermanager\` was already handled at cutover). One-time
    ops task, not product code. **Requires explicit step-by-step user approval.**

---

## API additions (summary)

| Method & path | Purpose | § |
|---|---|---|
| `POST /api/scenarios/resolve` | scenarios for a set of mod GUIDs (picker source, DB-first) | A4 |
| `POST /api/servers/{id}/config/preview` | generated config for an *unsaved* draft | A2 |
| `POST /api/servers/{id}/clone` | duplicate a definition + its mod set | A6 |
| `POST /api/servers/{id}/schedule-restart` · `GET` · `DELETE` | arm / read / cancel a warned restart | D23 |
| `POST /api/mods/{guid}/download` | force (re)download a mod version → job | B11 |
| `DELETE /api/mods/{guid}/local` | remove a mod's on-disk files (orphan cleanup) | B12 |
| `GET /api/storage` | free space, per-mod size, orphan + kept-as-dependency lists | B12 |
| `GET/POST /api/modpacks` · `GET/PATCH/DELETE /api/modpacks/{id}` | modpack CRUD | C14 |
| `POST /api/modpacks/{id}/apply/{server_id}` | apply a pack to a definition | C16 |
| `POST /api/modpacks/from-server/{server_id}` | create a pack from a definition | C17 |
| `GET /api/modpacks/{id}/export` · `POST /api/modpacks/import` | pack JSON transfer | C18 |
| `GET /api/settings` · `PATCH /api/settings` | runtime settings (nightly, spam patterns) | E24 |
| `POST /api/auth/password` | self-service password change | E26 |
| `GET /api/backup/export` · `POST /api/backup/import` | full definitions + packs JSON | F29 |

Existing routes that just need a UI (no backend change): `POST/PATCH/DELETE /api/servers`,
`POST/DELETE /api/servers/{id}/mods/{guid}/pin`, `POST /api/mods/add`, `GET /api/mods/search`,
`GET /api/mods/{guid}`, free-form `POST /api/servers/{id}/rcon` (`#kick`/`#ban` already allowed).

Existing route that needs a **behaviour fix**: `PATCH /api/servers/{id}` — pin preservation in
`_apply_mods` (fact #1).

## Data-model changes

- **New** `app_settings` — singleton (`id = 1`): `nightly_check_enabled bool`,
  `nightly_check_hour int`, `log_spam_patterns` (JSON string array), timestamps.
- No change to `servers`, `server_mods`, `modpacks`, `modpack_items`,
  `server_config_revisions`, `mods`, `engine`, `jobs`, `users` — all already sufficient.
- Migration: one Alembic revision adding `app_settings` (or `create_all` picks it up in the
  current default mode).

## Testing

The backend has a real pytest suite (`backend/tests/`, 13 modules). Every backend story in this
sprint carries tests in the same style — route tests against the app (see
`test_phase3b_routes.py`) and unit tests for logic that can be exercised without the network.
Specifically required: pin preservation across a mod-set replace, the orphan closure rule, the
free-space 409, the modpack apply/replace pin report, and the settings-driven spam filter. The
frontend stays untested (no harness in Sprint 1; adding one is a Non-goal).

## Cut from this sprint

- **Config-revision diff, rollback and UI** (was §D20–25, plus `GET/POST
  /api/servers/{id}/revisions*`). Reason: the stored snapshot is the *generated `config.json`*
  (`supervisor.py:142`), which is a lossy projection of the definition (fact #8) — disabled mods
  vanish, `game_properties` cannot be separated from the defaults, `extra_config` cannot be
  separated from what it overrode, pin metadata is absent, and the whole `rcon` block is missing
  when RCON is off. A rollback built on it would restore a definition that never existed.
  **Clone (§A6) is the experiment workflow instead.**
  The start-time snapshot write in `supervisor.py:138-146` **stays as-is** — it is free, passive,
  and a useful forensic record. No snapshot-on-save is added, no snapshot format changes, no
  migration.

## Non-goals (explicitly out of scope for Sprint 2)

- **Config-revision diff / rollback** — cut, see above.
- **Importing the 9 un-migrated `REFORGER_*.json` definitions** — they stay in
  `Z:\_archive\armaservermanager-2026-09-02.zip`. If one is ever wanted back it is re-created by
  hand in the new server-create form (§A1), which this sprint delivers.
- **Multi-user / roles** — stays a single admin account; only password change is added.
- **Public read-only status page** — everything stays behind the JWT.
- **In-app mod *content* browsing** (screenshots, changelogs beyond `summary`/tags).
- **Automated scheduled *backups*** to disk/off-box — export is on-demand only.
- **Editing the engine branch / betas** — `public` branch only, as in Sprint 1.
- **RCON commands beyond the BattlEye whitelist** the client already enforces.
- **A frontend test harness.**

## Verification

1. **Server CRUD** — create a 5th definition from scratch in the UI, edit its scenario via the
   picker and a `game_properties` value via Advanced, confirm the live preview matches the
   generated `config.json` after saving, start it, stop it, delete it. All without touching the
   API directly.
2. **Edit while running** — with a server running, change its `max_players`, confirm the save
   succeeds, the banner appears, the live process is unaffected, and the new value takes effect
   on the next start.
3. **Mod set** — on a definition, add two library mods, reorder them by drag, disable one,
   save, re-open — order and enabled state persist; pre-flight re-runs.
4. **Pin survives a reorder** — pin an assigned mod, then drag-reorder the set and save;
   confirm the pin is still there (fact #1 regression guard), the generated config still emits
   its `version`, and unpin works.
5. **Clone** — duplicate definition #2, confirm the copy has the same mods/order **and pins**
   and no runtime history, edit the clone without touching the original, delete the clone, and
   confirm starting the clone is refused while another server runs.
6. **Add mod** — add a mod by Workshop URL and by bare GUID; run a Workshop search and add a
   result. Each lands enriched.
7. **Mod detail** — open a mod with dependencies (e.g. RHS Status Quo) — version table,
   dependency tree with `via`/`state` badges, and "used by" all render.
8. **Force re-download** — trigger a re-download of one small mod, watch it progress on the
   Jobs page, confirm no full-cache re-fetch.
9. **Storage** — the storage view shows free space and per-mod sizes; an unreferenced mod
   appears as an orphan; **a dependency-only mod appears under kept-as-dependency, not as an
   orphan**; removing a real orphan flips `is_local` and frees the space; a download that would
   overflow free space is refused with a clear message.
10. **Modpacks** — create a pack, drag-order it, apply it to a definition (replace and append),
    confirm a surviving pinned mod keeps its pin and the response names any pin dropped with its
    mod, create a pack *from* a definition, export it to JSON, delete it, re-import the JSON.
11. **RCON** — with a server running: player table renders; scheduled-restart armed for
    2 minutes with warnings at 60/10 s sends the `#say` lines and restarts; reloading the page
    mid-countdown still shows it; cancel works; a backend restart clears it.
    (Kick/ban validated against the endpoint even with zero players connected.)
12. **Settings** — toggle the nightly check on/off and confirm the scheduler task starts/stops
    in the logs; change the admin password, re-login with the new one, and restart the backend
    to confirm the change persists; add a spam pattern and confirm the **console WebSocket**
    stops emitting matching lines (not just that the UI hides them).
13. **Backup** — export, delete a definition and a modpack, import the backup with
    `dry_run` first then for real, confirm both are restored and a running server was untouched.
14. **Hardening** — a cross-origin `fetch` from a disallowed origin is blocked; the app still
    works from `https://armaserver.badis.net` and from `http://192.168.1.10:18090`.
15. **Regression** — the Sprint 1 checks still pass: 4 migrated definitions keep their
    `warn / warn / blocked / blocked` verdicts; server 1 starts, reaches
    `Starting RPL server`, A2S + RCON respond, stops clean; the single-server rule holds.
    The reformat (§G32) changed no behaviour: every Sprint 1 page still works.
