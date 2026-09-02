# Reforger Manager — Sprint 2 implementation stories

> Context, decisions, code-level facts and verification live in [PLAN.md](PLAN.md).
> `§n` references are PLAN.md feature numbers; `fact #n` references its
> *Code-level facts the stories must respect* section.

> **Approval gate — see `Z:\CLAUDE.md`.** Only **S20** touches the NAS destructively and it
> requires explicit, step-by-step confirmation immediately before each command. Everything else
> in this sprint is code in `Z:\reforger-manager\` plus container rebuilds, which follow the
> normal deploy flow.

## Shape of the sprint

Twenty stories in six phases. The ordering is not arbitrary:

- **Phase 0** reformats the frontend *before* anything is built on it, so the large mechanical
  diff never interleaves with feature diffs.
- **Phase 1** fixes the two backend defects that every later story would otherwise inherit
  (pin destruction, no draft preview), so the mod-set and modpack UIs are built on a correct
  transport.
- **Phases 2–5** are independently shippable. If the sprint has to stop early, it stops at a
  phase boundary with a working app.

Each story lists **Deliver**, **Done when**, and **Watch out** (the trap that makes this story
non-obvious). Backend stories carry pytest coverage in the style of `backend/tests/`; the
frontend stays untested per PLAN Non-goals.

| Phase | Stories | Theme |
|---|---|---|
| 0 | S1 | Frontend groundwork |
| 1 | S2–S3 | Backend correctness prerequisites |
| 2 | S4–S8 | Server definitions in the UI |
| 3 | S9–S12 | Mod library completion |
| 4 | S13–S15 | Modpacks |
| 5 | S16–S20 | RCON, settings, backup, hardening, ops |

---

## Phase 0 — Frontend groundwork

### S1 — Reformat and split the frontend · §G32

**Deliver.** Reformat every file under `frontend/src/` to conventional multi-line formatting.
Split the multi-component files into one component per file:

- `pages/ServerDetail.tsx` → the page shell plus `components/server/{ConfigPanel,ModsPanel,
  ConsolePanel,PlayersPanel,HistoryPanel}.tsx`.
- `pages/Settings.tsx` → the page shell plus `components/settings/{SessionCard,AppearanceCard,
  EngineCard,HealthCard}.tsx`.
- `pages/Servers.tsx` → the page plus `components/server/ServerRow.tsx`.
- `pages/Mods.tsx`, `pages/Jobs.tsx`, `pages/Dashboard.tsx`, `pages/Login.tsx` → reformat;
  split only where a file holds more than one exported component.

Shared types currently declared inline in `ServerDetail.tsx` (`DetailServer`, `ConfigResponse`,
`Preflight`, `LogLine`, `LogResponse`, `Player`, `Stats`) move to `lib/api.ts` alongside the
existing `Server` / `Engine` types, since Phase 2–5 stories import them.

**Done when.** `npm run build` passes with no new TypeScript errors, and every Sprint 1 page
renders and behaves identically (PLAN verification 15). Zero behaviour changes in the diff —
if a rename or a `useEffect` dependency looks tempting, it belongs in a later story.

**Watch out.** `ConsolePanel`'s `useEffect` (`ServerDetail.tsx:82`) has `[id]` as its dependency
list while closing over `tailLimit` and the severity/spam state; the current code relies on that.
Reformatting must not "helpfully" widen the dependency array — that would reopen the socket on
every filter change. Same for the deliberate comment at line 80 about the unbounded log
endpoint: keep it.

---

## Phase 1 — Backend correctness prerequisites

### S2 — Preserve per-server pins across a mod-set replace · §A5, fact #1

**Deliver.** `_apply_mods` (`api/servers.py:75`) currently calls `server.mods.clear()` and
rebuilds from the payload, so any `pinned_version` / `pinned_at_build` / `pinned_reason` not
echoed back by the caller is destroyed, and `pinned_at` is destroyed unconditionally
(`ServerModIn` has no such field). Change it to index the existing rows by `mod_guid` and, for
every GUID that survives the replace, carry the four `pinned_*` columns forward unless the
incoming item explicitly sets `pinned_version`. GUIDs that leave the set lose their pin with
the row, as they should.

**Done when.** Tests cover: a PATCH with `mods[]` omitting pin fields keeps the pins; a PATCH
that explicitly sets `pinned_version` overwrites it; a PATCH that drops a GUID removes its pin;
a pure reorder (same GUIDs, new `load_order`) keeps every pin and every `pinned_at`.

**Watch out.** This is the single highest-consequence fix in the sprint — S5 (mod-set UI) and
S15 (modpack apply) both ride on it, and the failure is silent: the config still generates, it
just quietly stops pinning. Land it and its tests before either UI story starts.

### S3 — Draft config preview endpoint · §A2, fact #2

**Deliver.** `POST /api/servers/{id}/config/preview` taking a `ServerUpdate` body. It loads the
row, applies the patch to a **detached / non-flushed** copy, runs `build_config` +
`server_mod_entries`, and returns the same `ServerConfigOut` shape as the GET — without
persisting anything and without writing the config file.

**Done when.** A test asserts the preview reflects the body, and that the DB row and
`CONFIGS_DIR/<id>.json` are unchanged afterwards.

**Watch out.** The obvious implementation mutates the loaded ORM object and never commits — but
the session is a request-scoped `AsyncSession` and any later flush in the same request would
write those mutations. Build the preview from a copy, or `session.expunge` before mutating.
Also honour `mods` in the body: the Mods tab's unsaved working copy must be previewable too.

---

## Phase 2 — Server definitions in the UI

### S4 — Server config form: create, edit, delete · §A1, §A2, §A3

**Deliver.** One reusable `ServerForm` component driving three entry points: a "New definition"
button on `/servers` (`POST /api/servers`, defaults from `ServerBase`), the *Config* tab turned
editable (`PATCH /api/servers/{id}`), and a confirm-dialog delete on server-detail
(`DELETE /api/servers/{id}`). Common fields and the collapsed *Advanced* section exactly as
PLAN §A2 lists them, with the two raw-JSON editors validating before save. Live preview panel
fed by S3, debounced.

Editing is **allowed while running**, with a *"this server is running — changes apply on next
start"* banner. Delete stays refused while running (the API already 409s; surface the message).

**Done when.** PLAN verification 1 and 2 pass end to end.

**Watch out.** `game_properties` is a merge, not a replacement: `build_config` overlays it on
`DEFAULT_GAME_PROPERTIES` (`config_gen.py:33`). The form's eight known keys must round-trip
only keys the operator actually set — writing all eight back on every save silently converts
"inherits the default" into "pinned to today's default", which will drift when the defaults
change. The Advanced JSON editor must show only the keys *not* covered by the form, and the two
halves must merge on save without either clobbering the other.

### S5 — Mod set management with drag-order and pins · §A5

**Deliver.** The *Mods* tab becomes editable: searchable multi-select add from the library
(`GET /api/mods?local=true`, toggle to include non-local) showing size and the dependency
additions from `GET /api/mods/{guid}` → `dependency_tree`; remove; per-mod enable/disable;
`@dnd-kit/sortable` load-order drag; inline pin/unpin per assigned mod
(`POST` / `DELETE /api/servers/{id}/mods/{guid}/pin`). All edits mutate a local working copy;
one "Save mod set" button issues a single `PATCH` with the full `mods[]`. Pre-flight re-runs
after save. Add `@dnd-kit/core` + `@dnd-kit/sortable` to `package.json`.

Extract the sortable list as `components/mods/SortableModList.tsx` — S14 reuses it verbatim for
modpack items.

**Done when.** PLAN verification 3 and 4 pass.

**Watch out.** Two things. (a) The UI must round-trip the `pinned_*` fields it received even
though S2 also protects them server-side — belt and braces, because the failure is invisible.
(b) Pin/unpin are immediate API calls that return a fresh `ServerOut` while the mod set is an
unsaved working copy; pinning mid-edit will otherwise blow away the operator's pending reorder.
Merge the pin response into the working copy rather than replacing it, or disable pin controls
while the set is dirty and say why.

### S6 — Scenario picker · §A4

**Deliver.** New router `app/api/scenarios.py` with `POST /api/scenarios/resolve` `{guids: []}`
→ `[{game_id, name, game_mode, player_count, mod_guid, mod_name, source: "db"|"api"}]`.
**DB-first**: read `mod_scenarios` for each GUID; call `workshop.get_scenarios` only for GUIDs
with no cached rows and persist what comes back. A GUID that 404s or times out is reported in a
`failed: [{guid, reason}]` list rather than failing the request. Frontend: a `<select>` in the
Config form fed by the definition's current mods, plus a free-text field that always wins when
non-empty.

**Done when.** The picker lists scenarios for a mods-bearing definition; a pasted
`{GUID}Missions/x.conf` overrides the dropdown; a definition containing the known-404 mod
(`658756C5760E94DE`, Sprint 1 server 9) still returns the other mods' scenarios.

**Watch out.** Fact #7 — the Workshop client is 60/min with burst 20. A 20-mod definition that
fans out on every form render will trip the limiter and stall the form. DB-first is not an
optimisation here, it is the requirement. Also `docs/REF.md`: offline-scanned gameIds are not
always authoritative, which is exactly why the free-text override exists — do not "improve" the
picker by removing it.

### S7 — Clone a definition · §A6

**Deliver.** `POST /api/servers/{id}/clone` `{name}` deep-copying the `servers` row and its
`server_mods` **including pins**, and excluding `config`, `config_revision`, `config_revisions`,
and every runtime column (`is_running`, `pid`, `last_state`, `last_exit_code`,
`last_diagnosis`, `last_started_at`, `last_stopped_at`). "Duplicate this definition" button on
server-detail, navigating to the new definition.

**Done when.** PLAN verification 5 passes.

**Watch out.** This is the sprint's replacement for config-revision rollback (PLAN *Cut from
this sprint*), so the copy must be genuinely independent — a shared `ServerMod` row or a copied
`config_revision` counter would make "edit the copy, delete if bad" unsafe. Also: ports are
copied verbatim, so the clone collides with its source on `bind_port` / `a2s_port` /
`rcon_port`. The single-server rule means only one can run at a time so this is not a runtime
conflict, but the form should flag the duplicate ports rather than pretend they are fine.

### S8 — Favourites · §A7

**Deliver.** Star toggle on `/servers` rows and the server-detail header, writing
`is_favourite` via `PATCH`. Favourites sort first in the list.

**Done when.** Toggling persists across a reload; favourites sort first; the toggle works while
the definition is running (it is not a config field).

**Watch out.** Small story, one trap: sending the whole form body for a star toggle would drag
`mods[]` through `_apply_mods` for no reason. PATCH only `{is_favourite}`.

---

## Phase 3 — Mod library completion

### S9 — Add a mod, and Workshop search · §B8, §B9

**Deliver.** "Add mod" control on `/mods` accepting a Workshop URL or a bare 16-hex GUID
(`POST /api/mods/add`, which already extracts the GUID by regex). Inline search box
(`GET /api/mods/search?q=`) rendering name / summary / latest version / workshop link, each row
with an "Add" button funnelling into the same endpoint.

**Done when.** PLAN verification 6 passes.

**Watch out.** `POST /api/mods/add` returns 404 for a mod that is deleted, blocked *or* private
— the API cannot distinguish them (Sprint 1 decision, `mods.py:262`). Surface the endpoint's
wording; do not invent a specific reason in the UI. A 502 means the Workshop API is down, which
is a retry, not a bad GUID — the two must read differently.

### S10 — Mod detail view · §B10

**Deliver.** A detail page or drawer on `/mods/{guid}` backed by `GET /api/mods/{guid}`:
summary, tags, the `versions[]` table with per-version `gameVersion`, the `dependency_tree`
with `via: api|gproj|unknown` and `state` badges, and `used_by`. Library pin/unpin
(`POST`/`DELETE /api/mods/{guid}/pin`) and verify live here.

**Done when.** PLAN verification 7 passes — RHS Status Quo renders all four sections.

**Watch out.** `get_mod_detail` swallows both `ModNotFound` and `WorkshopError` into
`versions = []` (`mods.py:203-205`), so an empty version table means either "no versions" or
"the API was unreachable". Do not render that as an authoritative "no versions available".
Distinguish it if you can, hedge the wording if you cannot.

### S11 — Force re-download, with a free-space guard · §B11, §B13

**Deliver.** `POST /api/mods/{guid}/download` `{version?}` enqueuing the registered
`mod_download` job with `{guids: [guid], versions: {guid: version}}` (fact #6). "Re-download"
button on the mod row and detail. Free-space guard shared by this route,
`POST /api/mods/updates/apply` and `POST /api/servers/{id}/mods/update/apply`: sum `Mod.size`
for the affected GUIDs, compare against `shutil.disk_usage(mods_dir).free`, return 409 with the
projected and available sizes when it would not fit. The UI shows the message before enqueuing.

**Done when.** PLAN verification 8 passes, and a test asserts the 409 with a stubbed free-space
value.

**Watch out.** `Mod.size` is the Workshop package total and is `NULL` for anything unenriched —
a sum over NULLs must not silently become "0 bytes, plenty of room". Treat unknown sizes as
unknown and say so in the guard's message rather than guaranteeing a fit you cannot compute.

### S12 — Storage and orphan management · §B12

**Deliver.** `GET /api/storage` → `{mods_path, free_bytes, total_bytes, per_mod: [{guid, name,
bytes}], orphans: [...], kept_as_dependency: [{guid, required_by: [...]}]}`.
`DELETE /api/mods/{guid}/local` deletes the addon dir, sets `is_local = false`, keeps the DB
row. New storage section/page: free-space bar, per-mod size table sorted desc, orphan list with
"Remove from disk", and the kept-as-dependency list as read-only context.

**An orphan is `is_local` AND absent from `server_mods` AND absent from `modpack_items` AND
absent from the resolved dependency closure of both.** Build the closure with
`mods.resolve.resolve_dependencies` over the union of assigned and packed GUIDs.

**Done when.** PLAN verification 9 passes — specifically, a dependency-only mod appears under
`kept_as_dependency`, not as an orphan.

**Watch out.** The closure term is the whole story. Without it, a mod that exists only as a
dependency of an assigned mod (RHS bases, CBA-likes) is in no `server_mods` row and would be
offered for deletion — the UI would invite the operator to break a working server. Second:
`DELETE …/local` is the only route in this app that removes files. Re-check the orphan condition
server-side at delete time (the list may be stale), refuse while any server runs, and resolve
the addon path through the scanner's `addons_root()` rather than concatenating strings — a GUID
that does not resolve to a directory inside it must 400, never `rm -rf` a computed path.

---

## Phase 4 — Modpacks

### S13 — Modpack CRUD API · §C14

**Deliver.** New router `app/api/modpacks.py` and `schemas/modpack.py`: `GET /api/modpacks`,
`POST /api/modpacks` `{name, description, items: [{mod_guid, load_order}]}`,
`GET /api/modpacks/{id}`, `PATCH /api/modpacks/{id}`, `DELETE /api/modpacks/{id}`. Register in
`main.py`.

**Done when.** Route tests cover create / read / list / patch-items / delete, and the
`name` unique constraint and the `(modpack_id, mod_guid)` unique constraint surface as 409s
rather than 500s.

**Watch out.** `Modpack.name` is `unique=True` and `ModpackItem` has
`uq_modpack_item(modpack_id, mod_guid)` (`models/modpack.py`). A duplicate GUID inside one
POSTed `items[]` raises an IntegrityError from the flush, not a validation error — catch both
in the schema and at the route.

### S14 — Modpack UI · §C15, §C19

**Deliver.** The stub `/modpacks` page becomes a real page: pack list, create/edit form with a
drag-order mod multi-select reusing `SortableModList` from S5, per-pack **Apply to…**,
**Export**, **Delete**, and a global **Import** button. "Save current mod set as a pack" on
server-detail.

**Done when.** A pack can be created, reordered by drag, and edited from the UI alone.

**Watch out.** S5's list component was built around `ServerMod` (which has `enabled` and pins);
a `ModpackItem` has neither. Parameterise the component rather than forking it, or the two
drag lists will drift apart the first time either is touched.

### S15 — Apply, create-from-server, import/export · §C16, §C17, §C18

**Deliver.** `POST /api/modpacks/{id}/apply/{server_id}` `{mode: "replace"|"append"}` — refused
while that server runs, **preserving pins on GUIDs that survive the apply** (fact #1), with the
response naming any pin dropped because its mod left the set.
`POST /api/modpacks/from-server/{server_id}` `{name, description}` snapshotting the current mod
set and order; pins are **not** carried into the pack (a pack is a mod list, not a version
lock) and the response says so when the source had pins.
`GET /api/modpacks/{id}/export` → `{name, description, items: [...]}`;
`POST /api/modpacks/import?on_conflict=rename|replace|error`.

**Done when.** PLAN verification 10 passes.

**Watch out.** `apply` in `replace` mode is the second path through the destructive mod-set
replace, so it inherits fact #1 — reuse the S2-fixed helper rather than writing a second
clear-and-rebuild here. And `append` must not duplicate a GUID already assigned
(`uq_server_mod`); decide explicitly whether an existing GUID keeps its current `load_order` or
moves to the pack's, and write it down in the route docstring.

---

## Phase 5 — RCON, settings, backup, hardening, ops

### S16 — Player management and scheduled restart · §D20–23

**Deliver.** *Players* tab: table of name / id / IP / ping from `GET /api/servers/{id}/players`,
auto-refreshing while the tab is open; per-row **Kick** / **Ban** buttons issuing
`POST /api/servers/{id}/rcon` `{command: "#kick N"}` / `"#ban N"` behind a confirm; the existing
`#say` broadcast and `#restart` / `#shutdown` confirms stay.

Scheduled restart: `POST /api/servers/{id}/schedule-restart` `{in_seconds, warn_at: [300,60,10]}`,
`GET` (so a page reload recovers the countdown) and `DELETE`. One cancellable
`asyncio.Task` on the supervisor — a single running server means a single schedule. It sends
`#say` at each `warn_at` offset then `#restart`. Countdown + cancel in the Players tab.

**In-memory only**: cancelled when the server stops, does not survive a backend restart, and the
GET reports `armed: false` in those cases rather than a stale countdown. A warning that fails to
send (RCON down) is logged and does not abort the restart.

**Done when.** PLAN verification 11 passes.

**Watch out.** The RCON whitelist (`rcon/client.py:236`) is `re.fullmatch(r"#(?:kick|ban)\s+\d+")`
— exactly one integer, no reason string, no name. A "Kick with reason" input will be rejected by
the client, so do not build one. The player `id` from `parse_players` is that integer; pass it
through unchanged. Second: `#restart` terminates the process, and the supervisor's exit watcher
will see it — confirm it is classified as an intentional stop, not a crash, or the Dashboard will
report a crash after every scheduled restart.

### S17 — Runtime settings: app_settings, nightly toggle, password, spam patterns · §E24–28

**Deliver.**
- `app_settings` singleton model (`id = 1`): `nightly_check_enabled`, `nightly_check_hour`,
  `log_spam_patterns` (JSON string array), timestamps. One Alembic revision.
- `GET /api/settings` / `PATCH /api/settings`, falling back to env defaults when the row is
  absent.
- Promote `NightlyCheckScheduler` from a `lifespan` local (`main.py:206`) to a module-level
  singleton so `PATCH /api/settings` can `start()` / `stop()` it; `lifespan` keeps only initial
  state and shutdown (fact #4).
- `POST /api/auth/password` `{current_password, new_password}` — verify, re-hash, update, keep
  the session valid.
- `logview.is_spam_line` reads the settings-backed pattern list instead of its module constant,
  cached in-process and invalidated on `PATCH /api/settings` (fact #3). Delete the frontend's
  duplicate `isSpam()` (`ServerDetail.tsx:100`) and trust the backend `is_spam` flag.
- Settings page: nightly toggle + hour, password form, spam-pattern editor. Remove the
  "backend-not-yet-exposed" card (`Settings.tsx:87-92`).

**Done when.** PLAN verification 12 passes — including that the change survives a backend
restart and that the **console WebSocket** stops emitting a newly-added spam pattern.

**Watch out.** The spam filter is the part that is easy to get wrong: `is_spam_line` is what
filters the WebSocket (`api/servers.py:333`) and `GET /api/servers/{id}/log`. Changing only the
frontend copy would look like it works — new lines would be hidden in the UI — while the backend
kept streaming them. Also `_SPAM_PATTERNS` is compared lowercased against a lowercased line
(`logview.py:154`); keep that contract when the source becomes a DB column, and seed the row
with today's `thermalProfileDefault.conf` so behaviour is unchanged on first boot.

### S18 — Backup export and import · §F29–31

**Deliver.** `GET /api/backup/export` → one JSON document: every server definition (fields +
`server_mods` including pins) and every modpack. Excludes runtime state, the `engine` row and
the disk-derived mod library. `POST /api/backup/import?dry_run=true` returns a plan (created /
replaced / skipped, by name); `dry_run=false` applies it. Keyed by name,
`on_conflict=skip|replace` (default `skip`), never touching a running server. Settings page:
"Download backup" and "Restore from file" with the dry-run preview shown before confirming.

**Done when.** PLAN verification 13 passes.

**Watch out.** The export carries `game_password`, `admin_password` and `rcon_password` in
cleartext — it has to, or a restore produces a server nobody can administer. Say so plainly in
the download UI; do not quietly redact them into an unusable backup. Second: `replace` on a
definition goes through the mod-set replace path again (fact #1) — reuse the S2 helper.

### S19 — CORS hardening · §G33

**Deliver.** Set `CORS_ORIGINS` in `.env` to `https://armaserver.badis.net`,
`http://192.168.1.10:18090` and `http://localhost:5173`; drop the `["*"]` default from
`core/config.py:74`. `allow_credentials` stays true, now valid with an explicit list. Update
`.env.example`.

**Done when.** PLAN verification 14 passes.

**Watch out.** The SPA is served same-origin by FastAPI (`main.py:249`), so this changes nothing
for normal use — it is hygiene, not a live fix, and should be described that way. The real risk
is the reverse: an origin list that omits `http://localhost:5173` breaks local development the
next time someone runs `npm run dev`.

### S20 — Old-stack leftover cleanup (NAS) · §G34

> **This is the only story that deletes anything on TrueNAS. `Z:\CLAUDE.md` applies in full:
> every command below needs explicit user confirmation immediately before it runs, and approval
> for one command never carries to the next.**

**Deliver.** Confirm a same-day ZFS snapshot of `Apps/docker` exists. Archive
`/mnt/Apps/docker/armaservermanager/steam/logs` to
`Z:\_archive\armaservermanager-steam-logs-2026-09-02.tar.zst`. Verify the archive reads back.
Then, as a **separate** approval, `rm -rf /mnt/Apps/docker/armaservermanager/`.

**Done when.** The archive exists and is readable, the directory is gone, and disk usage
reflects it.

**Watch out.** Sequence and separateness are the whole point: snapshot check, then archive, then
verify the archive, then — only then, and only with a fresh confirmation — delete. If any step
is unclear, stop and describe rather than proceed. This story delivers no product value and can
be dropped from the sprint without affecting anything else; it is here only because PLAN §G34
lists it.

---

## Story dependency map

```
S1 (reformat) ──> every frontend story

S2 (pin preservation) ──> S5 (mod set UI)
                     └──> S15 (modpack apply)
                     └──> S18 (backup import/replace)

S3 (draft preview) ──> S4 (config form)

S4 (config form) ──> S6 (scenario picker feeds the form)
                └──> S7 (clone navigates into the form)

S5 (SortableModList) ──> S14 (modpack drag list)

S13 (modpack CRUD) ──> S14 (UI) ──> S15 (apply / transfer)
S12 (storage)      ──> needs modpack_items for the orphan rule, so after S13

S16, S17, S19, S20 are independent of the rest.
```

## Deferred, and why

Recorded so it is not rediscovered mid-sprint. Full reasoning is in PLAN.md.

- **Config-revision diff and rollback** — the stored snapshot is the generated `config.json`,
  a lossy projection of the definition; a rollback built on it would restore a definition that
  never existed. Clone (S7) is the experiment workflow instead. The passive start-time snapshot
  write stays.
- **Importing the 9 un-migrated `REFORGER_*.json` definitions** — they stay in the Sprint 1
  archive; S4 delivers the form to re-create one by hand if ever wanted.
- **A frontend test harness** — none exists from Sprint 1 and adding one is out of scope.
