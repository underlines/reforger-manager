# Reforger Manager — Sprint 10 results

Status: **implemented, gates green, not yet exercised on a live stack.**

## Story-by-story

| Story | Agent | Verified |
|---|---|---|
| S1 discovery + mod drift | claude | 5 tests, own file green |
| S2 columns + migration | opencode·deepseek | model/schema/migration reviewed, full suite green |
| S3 persistence config emission | opencode·glm | 3 tests, own file green |
| S4 supervisor save flags | claude | 5 tests, own file green |
| S5 snapshot store | claude | 5 tests, own file green |
| S6 saves API router | claude | 3 tests, own file green |
| S7 MCP tools | opencode·glm | diff reviewed, full suite green |
| S8 Saves tab | claude | build clean, checkbox defaults spot-checked |
| S9 persistence form | opencode·glm | diff reviewed, build clean |
| S10 grace audit + results | claude (orchestrator) | this document |

Every story's changes were verified independently by the orchestrator (full
`pytest -q` and/or `npm run build` re-run after each landed), not taken on the
implementing agent's word.

Final state: **348/348 backend tests passing, frontend build clean.**

## Data-model changes

One Alembic migration, `0005_server_persistence_and_save_selection`, nine
columns on `servers` (all with `server_default`, existing rows migrate
cleanly): `persistence_enabled`, `auto_save_interval`, `save_retention`,
`load_session_save`, `keep_session_save`, `hive_id`, `save_mode`,
`save_pinned_uuid`, `save_selection_sticky`. No new tables — snapshot
metadata lives in on-disk `.json` manifests beside their archives.

## New surface

**Backend**
- `backend/app/servers/saves.py` — `discover()`, read-only save-point +
  mod-drift discovery.
- `backend/app/servers/save_snapshots.py` — create/list/rename/delete/restore
  snapshots, upload validation.
- `backend/app/api/server_saves.py` — 12 routes under
  `/servers/{id}/saves`.
- `backend/app/servers/config_gen.py` — emits
  `game.gameProperties.persistence`.
- `backend/app/servers/supervisor.py` — `-loadSessionSave` /
  `-backendFreshSession` argv, selection consumption on successful start.
- `backend/app/mcp/tools.py` — 9 new MCP tools mirroring the REST surface
  (binary download/upload routes excluded by design).
- Settings: `save_snapshot_max_per_server` (20),
  `save_upload_max_uncompressed_bytes` (500MB).

**Frontend**
- New **Saves** tab (`SavesPanel.tsx`) on the server detail page.
- Persistence fieldset in `ServerForm.tsx`.

## Verification performed

- `cd backend && pytest -q` — 348 passed, re-run after every story landed.
- `cd frontend && npm run build` — clean, re-run after every frontend-touching
  story landed.
- Each story's diff was read and spot-checked against its brief before being
  counted as done (exact config nesting, exception-to-status-code mapping,
  checkbox defaults, exclusion of the three runtime selection fields from
  `SERVER_CONFIG_FIELDS`, etc.) — not just "tests pass."
- F7 grace-period audit (non-destructive, log/disk evidence only, no server
  stopped/started for this sprint): see **Findings** below.

## Not exercised

- **No live/Docker verification.** `docker compose up -d --build` and the
  manual matrix from `PLAN.md` (arm → start → engine loads the save; arm
  while running → dialog; snapshot → delete → restore; download/upload
  round-trip; persistence fields round-trip; mod-drift badges on server 24's
  real save points) have **not** been run. Server 24 was live and running
  with the sprint's own service layer confirming genuine on-disk save data —
  deliberately not touched to avoid disrupting it. Running the matrix
  requires the user's go-ahead (rebuild + restart of the manager container is
  a step up in blast radius from anything sandboxed so far).
- No end-to-end test of the actual `-loadSessionSave <uuid>` /
  `-backendFreshSession` flags against a real engine process — S4's tests
  mock the spawn, per the story brief (launching a real engine binary was
  explicitly out of scope for a unit test).
- No test of `store_upload`'s multipart path against FastAPI's real
  `UploadFile` machinery end-to-end (S6's routing tests cover the 400/409
  mapping, not a real large-file upload).
- No test of the tar-based download routes producing an archive a real
  `tar` extracts correctly (only round-tripped through the same `tarfile`
  code that wrote it).

## Findings (F7 — shutdown-grace audit)

`stop_grace_seconds` defaults to 20.0 (`backend/app/core/config.py:72`),
governing the SIGTERM→SIGKILL window in `supervisor.stop()`. The sprint
planned this audit on the assumption — stated in the original research and
carried into `PLAN.md` — that Reforger writes a distinct save on clean
shutdown, making the grace period a data-loss parameter.

**That assumption did not hold up against the evidence.** Server 13's log for
a genuine clean `stop()` (SIGTERM, `state="stopped"`, `rc=0`) shows the full
teardown sequence — player disconnect, BattlEye, replication finish, engine
destroy — in about a dozen log lines, with **no persistence/save activity
anywhere in it**. Every save point on disk for both audited servers traces
cleanly to the periodic autosave timer instead. Server 24's most recent save
(`savepoint014`) landing seconds before its last recorded stop is coincidence
of autosave cadence (~10.5 min, consistent across the whole sequence), not a
shutdown-triggered save — and that particular "stop" was actually
`reconcile_on_startup()` clearing a stale `is_running` flag after a backend
restart, not a graceful `stop()` call at all, so it's not even good evidence
either way.

**Conclusion: no default change made.** Twenty seconds looks conservative for
what teardown actually does, and there is no observed save-on-exit step for
it to risk cutting off. A live test (trigger a real `stop()`, watch for a new
savepoint directory appearing between SIGTERM and process exit) would be the
only way to fully rule out a save-on-exit path under some other condition,
but nothing in the evidence available motivates changing the default speculatively.

## Observations

- S1's agent caught a wrong field name in its own brief
  (`server.scenario_game_id`, not `scenario_id`) and self-corrected — worth
  noting since that field name was propagated correctly into every later
  story's brief as a result.
- S6 (originally slotted for opencode·deepseek per the rotation) was moved to
  native Claude because it was the largest, most convention-heavy story in
  the sprint and both S7 and S8 were blocked on it landing clean — a
  deliberate rotation deviation, not a stall/restart.
- No agent stalled or required a restart this sprint — every dispatch
  produced real, passing code on the first attempt, verified independently
  each time before the next wave launched.

## How this sprint was implemented

Ten stories across four dependency waves. Wave 1: S1 (save discovery, native
Claude) and S2 (persistence/selection columns + migration, opencode·deepseek)
in parallel, no shared files. Wave 2: S3 (config emission, opencode·glm), S4
(supervisor flags, native Claude), S5 (snapshot store, native Claude), and S9
(persistence form, opencode·glm) — all four independent once S1/S2 landed.
Wave 3: S6 (saves API router) on native Claude, reassigned from its planned
opencode·deepseek slot for reliability given it was the convergence point for
S7 and S8. Wave 4: S7 (MCP tools, opencode·glm) and S8 (Saves tab, native
Claude) in parallel. S10 (this document, plus the F7 audit) closed the sprint
on the orchestrator directly. Every story's actual code was read and its
tests independently re-run by the orchestrator before the next wave was
dispatched — nothing was advanced on an agent's self-report alone.
