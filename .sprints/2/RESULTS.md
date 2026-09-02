# Reforger Manager — Sprint 2 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-02 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: complete

All 19 code stories (S1–S19) implemented, each landed on `main` after a green
frontend build and a green backend test suite. **S20** (NAS old-stack cleanup)
was deliberately skipped — see *Not done* below.

## Commits

| Wave | Commit | Stories | Theme |
|---|---|---|---|
| 1 | `7ac4c67` | S1, S2, S3, S13 | Frontend reformat/split + backend correctness prerequisites + modpack CRUD |
| 2 | `522a86e` | S4, S9, S10, S16 | Server config form, mod add/detail, players + scheduled restart |
| 3 | `474748a` | S5, S11, S12, S15, S17, S19 | Mod-set UI, storage/orphans, modpack transfer, runtime settings, CORS |
| 4 | `dbfab82` | S6, S7, S8, S18 | Scenario picker, clone, favourites, backup |

(Base commit for the sprint: `73eb138`.)

## Story-by-story

| # | Story | State | Notes |
|---|---|---|---|
| S1 | Reformat + split frontend | done | `src/` reformatted to conventional style; `ServerDetail` → `components/server/{Config,Mods,Console,Players,History}Panel.tsx`; `Settings` → `components/settings/{Session,Appearance,Engine,Health}Card.tsx`; `Servers` → `ServerRow.tsx`; shared types moved to `lib/api.ts`. Behaviour-neutral. |
| S2 | Preserve per-server pins across a mod-set replace | done | `_apply_mods` reconciles in place: `pinned_*` carry forward for surviving GUIDs unless the payload sets `pinned_version`. Also fixed a latent `uq_server_mod` IntegrityError on pure reorder. Tests: omit-keeps, explicit-overwrites, drop-removes, reorder-keeps-`pinned_at`. |
| S3 | Draft config preview endpoint | done | `POST /api/servers/{id}/config/preview` — `session.expunge` then apply a `ServerUpdate` (incl. `mods`) to the detached row, return `build_config` output, no persistence, no file write. |
| S4 | Server config form: create / edit / delete | done | Reusable `ServerForm` (create dialog on `/servers`, editable Config tab, delete confirm). Hybrid: structured common fields + collapsed Advanced with two validated raw-JSON editors. Per-key `game_properties` override so defaults are never written back. Debounced live preview via S3. "changes apply on next start" banner while running. |
| S5 | Mod-set management (drag-order + pins) | done | Editable Mods tab: searchable add-from-library with dependency preview, remove, enable/disable, `@dnd-kit` drag order, inline pin/unpin. One `PATCH` save; pre-flight re-runs. Pin response merged into the working copy so a pending reorder survives. `SortableModList<T>` extracted (reused by S14). Added `@dnd-kit/core` + `/sortable` + `/utilities`. |
| S6 | Scenario picker | done | `POST /api/scenarios/resolve` — **DB-first**: reads `mod_scenarios`, calls Workshop only for GUIDs with no cached rows, upserts on `(mod_guid, game_id)`. A failed GUID → `failed[]`, request still succeeds. `ScenarioField` is a `<select>` fed by the definition's mods + a free-text override that always wins. |
| S7 | Clone a definition | done | `POST /api/servers/{id}/clone` — deep-copies config columns + every `ServerMod` (incl. all `pinned_*`, `load_order`) as fresh rows; excludes `id`, runtime state, `config`, `config_revision`. "Duplicate this definition" button → navigates to the copy. Form flags a bind/a2s/rcon port already used by another definition (non-blocking). |
| S8 | Favourites | done | Star toggle on `/servers` rows and the detail header, `PATCH {is_favourite}` only. Favourites sort first (stable). Works while running. |
| S9 | Add a mod + Workshop search | done | "Add mod" (URL or bare 16-hex GUID) and an inline Workshop search on `/mods`, both funnelling `POST /api/mods/add`. 404 (deleted/blocked/private — indistinguishable) vs 502 (Workshop down) worded differently. |
| S10 | Mod detail view | done | `/mods/:guid` — summary, tags, `versions[]` (+ `gameVersion`), resolved dependency tree with `via`/`state` badges, `used_by`. Pin/unpin + verify. Empty `versions[]` hedged (could be unreachable Workshop). |
| S11 | Force re-download + free-space guard | done | `POST /api/mods/{guid}/download` → `mod_download` job. Shared guard (`mods/freespace.py`) on that route, `/mods/updates/apply`, `/servers/{id}/mods/update/apply`: sums known `Mod.size`, **NULL size treated as unknown** (409 "cannot guarantee"), otherwise 409 with projected vs free bytes. |
| S12 | Storage & orphan management | done | `GET /api/storage` — per-mod on-disk sizes (desc), free/total, `orphans`, `kept_as_dependency`. Orphan = `is_local` AND not in `server_mods` AND not in `modpack_items` AND not in the **offline dependency closure** of both. `DELETE /api/mods/{guid}/local` re-checks server-side, refuses while any server runs, path anchored under `addons_root()` (400 if it doesn't resolve inside). New `/storage` page + nav link. |
| S13 | Modpack CRUD API | done | `app/api/modpacks.py` + `schemas/modpack.py`: list/create/read/patch/delete. Duplicate GUID in one payload → 422; `modpacks.name` / `uq_modpack_item` → 409 (never 500). |
| S14 | Modpack UI | done | `/modpacks` is a real page: list, create/edit form with `SortableModList` drag order + add-from-library, per-pack Apply-to / Export / Delete, global Import (rename/replace/error). "Save current mod set as a pack" on server-detail. `SortableModList` reused verbatim, no fork. |
| S15 | Apply, create-from-server, import/export | done | `POST /api/modpacks/{id}/apply/{server_id}` (`replace` reuses the S2 `_apply_mods` helper and reports `dropped_pins`; `append` never moves/unpins an already-assigned GUID). `POST /api/modpacks/from-server/{server_id}` (pins not carried; `pins_note`). `GET .../export` + `POST /import?on_conflict=rename|replace|error`. |
| S16 | Player management + scheduled restart | done | Players tab: name/id/IP/ping table with auto-refresh; per-row Kick/Ban (`#kick N` / `#ban N`, no reason string) behind a confirm; `#say` / `#restart` / `#shutdown` kept. Supervisor gains one cancellable `asyncio.Task` schedule (`#say` at each `warn_at`, then `#restart`, marked as an intentional stop so it isn't read as a crash). `POST`/`GET`/`DELETE /api/servers/{id}/schedule-restart`; in-memory only; reload-surviving countdown + cancel in the UI. |
| S17 | Runtime settings | done | `app_settings` singleton + Alembic `0002`. `GET`/`PATCH /api/settings` (nightly enabled/hour, spam patterns), env fallback when the row is absent. `NightlyCheckScheduler` promoted to a module-level singleton toggled at runtime from the route. `POST /api/auth/password` (verify current, re-hash, keep session; new ≥ 8). `logview.is_spam_line` reads the DB-backed pattern list (in-process cache, invalidated on `PATCH`); frontend `isSpam()` duplicate deleted, Console trusts the backend `is_spam` flag. Settings "backend-not-yet-exposed" card removed; new nightly / password / spam-pattern cards. |
| S18 | Backup export / import | done | `GET /api/backup/export` — every server definition (fields + `server_mods` incl. pins, **cleartext passwords**) + every modpack; excludes runtime state, `engine`, the disk-derived mod library. `POST /api/backup/import?dry_run=&on_conflict=skip|replace` — dry-run returns a per-name create/replace/skip plan, `dry_run=false` applies it; `replace` reuses the S2 helper; a running server is forced to `skip` (in the plan and the real run). `BackupCard` on Settings, with a plain cleartext-password warning. |
| S19 | CORS hardening | done | `core/config.py` `cors_origins` default is now the explicit list (`https://armaserver.badis.net`, `http://192.168.1.10:18090`, `http://localhost:5173`); `["*"]` dropped. `allow_credentials` stays true. `CORS_ORIGINS` documented in `.env.example`. |

## Not done

- **S20 — Old-stack leftover cleanup (NAS)** — *skipped, by design.* It is the
  only story that deletes anything on TrueNAS, requires explicit step-by-step
  user approval per `Z:\CLAUDE.md`, and this checkout's `CLAUDE.md` states the
  NAS is not involved here (no SSH, no `Z:\` writes). STORIES.md: it "delivers
  no product value and can be dropped from the sprint without affecting
  anything else." Run it separately against the NAS when wanted.

## Data-model changes

- **New** `app_settings` (singleton `id = 1`): `nightly_check_enabled`,
  `nightly_check_hour`, `log_spam_patterns` (JSON string array), timestamps.
- Alembic: one new revision `0002_app_settings` (`down_revision = 0001_initial`).
  Single head. `create_all` also picks it up.
- No changes to any other table.

## New / changed API surface

New: `POST /api/scenarios/resolve` · `POST /api/servers/{id}/config/preview` ·
`POST /api/servers/{id}/clone` · `POST|GET|DELETE /api/servers/{id}/schedule-restart` ·
`POST /api/mods/{guid}/download` · `DELETE /api/mods/{guid}/local` · `GET /api/storage` ·
`GET|POST /api/modpacks` · `GET|PATCH|DELETE /api/modpacks/{id}` ·
`POST /api/modpacks/{id}/apply/{server_id}` · `POST /api/modpacks/from-server/{server_id}` ·
`GET /api/modpacks/{id}/export` · `POST /api/modpacks/import` ·
`GET|PATCH /api/settings` · `POST /api/auth/password` ·
`GET /api/backup/export` · `POST /api/backup/import`.

Behaviour fix: `PATCH /api/servers/{id}` — pin preservation in `_apply_mods` (S2).

New frontend deps: `@dnd-kit/core`, `@dnd-kit/sortable`, `@dnd-kit/utilities`
(the only runtime deps added, per the PLAN decision).

## Verification performed (local, no Docker)

- **Backend:** `DATABASE_URL=sqlite+aiosqlite:///:memory: python -m pytest` →
  **143 passed, 3 subtests passed** (baseline before the sprint: 57). Required
  coverage from the PLAN is present: pin preservation across a replace, the
  orphan closure rule, the free-space 409 (incl. the NULL-size case), the
  modpack apply/replace pin report, the settings-driven spam filter, clone
  independence, and the backup round-trip.
- **Frontend:** `npm run build` (`tsc -b && vite build`) green; `tsc -b --force`
  clean. No test harness (PLAN Non-goal).
- **Migrations:** `alembic upgrade head` applies; one head (`0002_app_settings`).
- **App boot:** `from app.main import app` imports; every new router is
  registered and its routes resolve.
- **Notes:** `backend/pytest.ini` was added (`norecursedirs = scratch-phase3a`
  — that dir holds a standalone smoke script with a module-level `sys.exit`
  that broke collection). `pytest` + `pytest-asyncio` were added to
  `backend/requirements.txt` (the suite imported them but they were unpinned).

## Still to verify on a real deployment (needs `docker compose up` + a server)

These are the PLAN "Verification" items that require a running stack / a live
game server and were **not** exercised here:

1. Create a 5th definition end to end; live preview matches the generated
   `config.json` after saving; start / stop / delete from the UI only. (PLAN 1)
2. Edit `max_players` while running — save succeeds, banner shows, live process
   unaffected, new value applies on next start. (PLAN 2)
3. Mod-set add/reorder/disable persists; pre-flight re-runs. (PLAN 3)
4. Pin survives a drag-reorder + save; generated config still emits `version`;
   unpin works. (PLAN 4)
5. Clone #2 → same mods/order **and pins**, no runtime history; starting the
   clone is refused while another server runs. (PLAN 5)
6–10. Add-mod (URL + GUID + search result), mod detail on RHS Status Quo,
   force re-download with no full-cache re-fetch, storage view (orphan vs
   kept-as-dependency, remove a real orphan, overflow refused), modpack
   create/drag/apply(replace+append)/from-server/export/re-import. (PLAN 6–10)
11. Live: player table; scheduled restart armed for 2 min with warnings at
   60/10 s sends the `#say` lines and restarts; reload mid-countdown still
   shows it; cancel works; a backend restart clears it. (PLAN 11)
12. Toggle nightly check on/off and see the scheduler task start/stop in the
   logs; change the admin password, re-login, restart backend → still changed;
   add a spam pattern and confirm the **console WebSocket** stops emitting
   matching lines (not just the UI). (PLAN 12)
13. Backup export → delete a definition + a modpack → import with `dry_run`
   then for real → both restored, running server untouched. (PLAN 13)
14. A cross-origin `fetch` from a disallowed origin is blocked; the app still
   works from `https://armaserver.badis.net` and `http://192.168.1.10:18090`.
   (PLAN 14)
15. **Regression:** the 4 migrated definitions keep their
   `warn / warn / blocked / blocked` verdicts; server 1 starts, reaches
   `Starting RPL server`, A2S + RCON respond, stops clean; single-server rule
   holds; every Sprint 1 page still works after the S1 reformat. (PLAN 15)

## How this sprint was implemented

Per the workflow now recorded in `CLAUDE.md` ("Implementing a sprint"): all
implementation delegated to sub agents across the native Sonnet agent + the two
opencode models (`deepseek-v4-flash`, `glm-5.3-flash`), max 3 concurrent, in
four dependency-ordered waves. Shared-file contention points (`_apply_mods` /
`api/servers.py`, `main.py` router registration, `ServerForm.tsx`) were
serialized or split into non-overlapping regions; router registration for the
storage / scenarios / backup routers and the S19 CORS change were done directly
during integration. Each wave was verified (full pytest + `npm run build`) and
committed before the next started.
