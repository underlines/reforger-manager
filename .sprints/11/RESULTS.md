# Reforger Manager — Sprint 11 results

Status: **complete**, backend-only. All 8 stories implemented, verified against
the full backend suite, the frontend build, and a live run of the local
Docker stack (real engine start, real Workshop API enrichment, real jobs) —
see "Live verification" below. Live testing surfaced and fixed two real
gaps: one in-scope (S6's `source` field wasn't wired into its API response)
and one pre-existing bug unrelated to any sprint-11 story (a `mod_sync`
crash on rescan) — see the two sections below the story table.

## Story table

| Story | Deliverable | Agent | Status |
|---|---|---|---|
| S1 | Pre-flight skips `ENGINE_BUILTIN_GUIDS`; older-declared-version → `warn`, not `blocked` | claude | Done |
| S2 | Scenario check in pre-flight (`warn`-only, names the correction on a path-alike mismatch) | deepseek | Done |
| S3 | `mod_sync` repair-warning pass, log-only, never rewrites `scenario_game_id` | glm | Done |
| S4 | `start_server` surfaces `preflight_blocked` in its response; still starts unconditionally | deepseek | Done |
| S5 | Batch mod downloads (`POST /mods/download`, `download_mods` MCP tool); single-GUID paths reimplemented as thin wrappers | claude | Done |
| S6 | `tail_log` prefers the newest `logs_*/console.log` session dir; falls back to the aggregate only when none exists; reports `source` | glm | Done |
| S7 | `update_server` omits `mods` by default (`mod_count` + `?include=mods`); `list_mods` gains `?compact=true`; MCP `list_mods` compact by default | deepseek | Done |
| S8 | Gates + this document | claude (orchestrator) | Done |

Agent rotation actually used: **3 native Claude subagents (S1, S5, S8),
3 opencode·deepseek (S2, S4, S7), 2 opencode·glm (S3, S6)** — 3/8 (37.5%)
Claude, 5/8 (62.5%) opencode, matching the requested ~33/67 split. No stalls,
no restarts — every agent's first run produced passing code.

## Gap found and fixed during live verification (in-scope, S6)

S6 added `LogFile.source` and computed it correctly inside
`backend/app/mods/logview.py`, but never wired it into the actual API
response: `get_log` in `backend/app/api/servers.py` called `current_log()`
only to check `.exists`, and the route's returned dict never included
`source`. S6's own unit tests only exercised `logview.current_log()`
directly, so this was invisible to `pytest` — every real caller (the
frontend console tab, the MCP log tool) was still blind to which file was
read, silently defeating the point of the story. Fixed by adding
`"source": log.source` to the response dict in `get_log`
(`backend/app/api/servers.py`), with two new route-level regression tests
in `backend/tests/test_logview_session_log.py`
(`GetLogRouteSourceTests`) that hit `GET /servers/{id}/log` through the
real ASGI app rather than calling `logview` functions directly. Confirmed
live: `GET /servers/2/log` now returns `"source":"session"` while the
container's server has an active session log.

## Bug found and fixed during live verification (out of sprint-11 scope)

Triggering a real `mod_sync` job against the live Postgres DB crashed it:

```
IntegrityError: duplicate key value violates unique constraint "uq_mod_scenario"
DETAIL: Key (mod_guid, game_id)=(62E960A6A1BA0985, {62E960A6A1BA0985}Missions/...) already exists.
```

Root cause, confirmed by `git blame`: `_upsert_local` in `backend/app/mods/sync.py`
(landed in `76895ac`, 2026-09-17, the day *before* this sprint — no sprint-11
story touches this function) deletes an offline-only `ModScenario` row and
unconditionally re-adds a new row for the same mod's freshly-scanned
scenario, even when its `game_id` is byte-for-byte unchanged since the last
scan. SQLAlchemy applies pending inserts before pending deletes within one
flush, so an unchanged game_id collides with itself on the
`(mod_guid, game_id)` unique constraint. This isn't an edge case — it's the
*normal* case on any second `mod_sync` run of a mod whose local scenario
list hasn't changed, i.e. nearly every real run.

Fixed in the same file: rows whose `game_id` the fresh scan still produces
are now left alone (no delete-then-recreate); only genuinely stale or
genuinely new rows are deleted/added. Added
`test_unchanged_offline_scenario_survives_a_second_rescan` to
`backend/tests/test_mod_sync_scenario_preservation.py`, confirmed it
reproduces the exact live error against the pre-fix code (via `git stash`)
and passes against the fix. Full suite re-verified at 396/396 after the fix,
and the live container re-verified with a real `mod_sync` run against
Postgres: `state: succeeded, enriched_ok: 5, errors: 0`.

## Data-model changes

None. The optional `mod_scenarios.source` column (PLAN §"Data-model changes")
was not taken — the `name IS NOT NULL` proxy stayed exact and cheap enough
that the extra migration wasn't worth it, per the PLAN's own "skip if tight"
guidance.

## New/changed API and MCP surface

- `POST /api/mods/download` — `{guids: list[str], versions?: dict[str,str]}`,
  one job for the whole batch. `POST /api/mods/{guid}/download` still works,
  now a thin wrapper over the same path.
- MCP `download_mods` tool mirrors the batch route; `download_mod` unchanged
  from a caller's perspective.
- `PATCH /api/servers/{id}` — response drops `mods`, adds `mod_count`, unless
  `?include=mods` is passed. `GET /api/servers/{id}` unchanged.
- `GET /api/mods?compact=true` — drops `required_by`/`thumbnail` (`versions`
  isn't on `ModOut` today; popped defensively). MCP `list_mods` now requests
  the compact shape by default (`ModListFilters.compact: bool = True`).
- `POST /api/servers/{id}/start` response gained `preflight_blocked: [...]`
  (always present, empty when clean) — the start itself is unconditional,
  unchanged from before.
- `GET .../log` (tail/log view) responses now carry which log file was
  actually read (`source: "session" | "aggregate"`) via `LogFile.source`.

## Behavior changes

- Pre-flight no longer reports `blocked` for `58D0FB3206B6F859` /
  `5614BBCCBB55ED1C` (the vanilla base-game GUIDs) under any circumstance —
  they're skipped before any Workshop lookup.
- Pre-flight's addon-version compatibility check now only `blocks` when an
  addon declares a **newer** game version than the installed engine; an
  **older** declared version now `warn`s (this was the exact shape of the
  false positive that hid the real WCS_Everon scenario bug on 2026-09-17).
- Pre-flight gained a scenario-id check (`warn` only, at most one check per
  report): silent on an API-verified match, warns on an offline-scan-only
  match, names the Workshop-verified correction on a path-alike mismatch
  under a different GUID, and warns generically when nothing matches at all.
- `mod_sync` now logs (not rewrites) a `WARNING` for any server whose
  `scenario_game_id` has no verified match but a single unambiguous
  path-alike verified scenario exists elsewhere in the library.
- `POST /servers/{id}/start` always runs pre-flight and reports blocking
  findings in its response; it does not refuse to start on them (Decision 3 /
  the sprint's explicit non-goal).

## Verification performed

```
cd backend && ./.venv/Scripts/python.exe -m pytest -q
  → 396 passed, 0 failed (baseline before this sprint: 363; sprint added 30
    new tests across the 7 new story test files, plus 3 more added during
    live verification: 2 route-level `source` regression tests in
    test_logview_session_log.py, 1 unchanged-rescan regression test in
    test_mod_sync_scenario_preservation.py)

cd frontend && npm run build
  → tsc -b && vite build succeeded, no errors (no frontend files were
    touched this sprint; this just confirms the pre-existing SPA still
    builds clean)
```

Each story's tests were also run in isolation by its implementing agent
before the full-suite pass, and every agent independently confirmed against
`git stash` / baseline counts that any concurrent-edit test noise mid-sprint
(from other stories landing in the same shared working tree at the same
time) was not caused by its own change.

### Live verification (Phase E, real Docker stack)

Ran against the local stack (`docker compose up -d --build`, real engine
build 24870635/1.8.0.13 already installed, real Postgres, real Workshop API
reachable from the container) rather than the production fleet — the local
dev DB has one server definition ("test", id 2), not the prod fleet's
13/22/24/32. Verified through a mix of the browser UI (chrome-devtools MCP)
and direct API calls:

1. **F1 (S1) live**: added Ronin AI (`6294F6D5EDD5CA66`, declares game
   version `1.2.1.173`) to the test server and did a real server start so
   the engine's console log populated `engine.installed_version`
   (`1.8.0.13`). Pre-flight returned `verdict: "warn"`, not `blocked`, with
   `Engine compatibility 6294F6D5EDD5CA66` at `warn` — this is the exact
   major-version-gap shape the sprint exists to stop false-blocking on. The
   base-game GUID `58D0FB3206B6F859` never appeared as a compat check at
   all, confirming the `ENGINE_BUILTIN_GUIDS` skip.
2. **Real engine start**: the same live start also exposed that Ronin AI
   genuinely fails to load on this engine build (`Addon loading failed`) —
   which is exactly pre-flight's `warn` copy ("verify against a live
   start") doing its job: it didn't false-block, and the live start is what
   caught the real incompatibility.
3. **S4 live**: `POST /servers/2/start`'s real JSON response carried
   `"preflight_blocked":[]` on a clean run.
4. **S2 + S3 live, against real Workshop data**: set the test server's
   `scenario_game_id` to the mod-GUID-guessed id
   (`{61B514B96692C049}Missions/ConflictPVERemixedVanilla2_Cain.conf`).
   Before a mod_sync, pre-flight correctly warned "came from a local mod
   scan". After triggering a real `mod_sync` job (which hit the live
   Workshop API and enriched real scenario data, uncovering the
   sync.py bug above), the same mission actually resolved to a
   *different*, Workshop-verified GUID
   (`{3BE30F418DCEE458}Missions/ConflictPVERemixedVanilla2_Cain.conf`) —
   S3 logged the discrepancy (`server 2 ('test'): configured scenario id
   ... matches no Workshop-verified scenario; the Workshop lists
   {3BE30F418DCEE458}...`), and pre-flight's S2 check independently picked
   up the same correction and named it in its `warn` detail. This is the
   server-32 incident shape reproduced against live data, not a fixture.
5. **F3 (S5) live**: `POST /mods/download` with two GUIDs
   (`6294F6D5EDD5CA66`, `5965550F24A0C152`) in one call; the job log shows
   exactly one `launching headless addon downloader` / one engine spawn
   handling both GUIDs, succeeding in ~9s.
6. **F4 (S6) live**: after the fix above, `GET /servers/2/log` returns
   `"source":"session"` while a session log directory exists, reading the
   newest `logs_*/console.log` rather than the stale aggregate.
7. **F5 (S7) live**: `PATCH /servers/2` without `?include=mods` omits
   `mods` and returns `mod_count`; `GET /mods?compact=true` entries lack
   `required_by`/`thumbnail`.
8. Crash diagnosis still works correctly: an earlier live start with an
   invalid empty `scenarioId` (before the scenario id was saved) was
   correctly detected and surfaced as `last_state: "crashed"`.

I also read the final diff for every touched file
(`backend/app/servers/preflight.py`, `backend/app/mods/sync.py`,
`backend/app/mods/logview.py`, `backend/app/api/servers.py`,
`backend/app/api/mods.py`, `backend/app/mcp/tools.py`,
`backend/app/schemas/mod.py`, plus the pre-existing test files each agent
had to adjust) and confirmed each matches its story's intent.

## Not exercised

1. The actual production fleet (servers 13/22/24/32 on TrueNAS) — this run
   verified against the local dev stack's one server definition instead;
   production TrueNAS/SSH is out of scope as always and was never touched.
2. `tail_log` following a live session across a *real mid-session* restart
   with an actually-running, successfully-loaded game (the live test
   server's addon set failed to load — see point 2 above — so the "restart
   while a session is genuinely live" case was exercised structurally via
   two distinct `logs_*` session directories and the newest-wins selection,
   but not with a fully up game world in between).

## Minor observations

- `ModListFilters.compact` defaulting to `True` for MCP but `list_mods`'s
  REST `compact` query param defaulting to `False` is intentional (S7's
  brief: MCP responses are the ones that hit the size ceiling in practice;
  REST callers, including the frontend, keep the full shape unless they ask
  otherwise) — worth remembering if a future story adds a frontend caller
  that assumes REST `list_mods` is already compact.
- `update_server`'s trimmed response is returned via a raw `JSONResponse`
  rather than through the declared `response_model=ServerOut`, since the
  trimmed shape (with `mod_count` and without `mods`) doesn't fit
  `ServerOut` as declared. This is consistent with how `list_mods` handles
  its own `?compact=true` case, but means neither trimmed shape is captured
  in the OpenAPI schema — acceptable for this sprint's ergonomics goal, but
  a future consumer generating a typed client from the schema won't see it.
- The optional `mod_scenarios.source` column from the PLAN was intentionally
  not added (see "Data-model changes" above).

## How this sprint was implemented

PLAN.md and STORIES.md already existed at the start of this session (written
in an earlier session from the 2026-09-17 WCS_Everon incident writeup), so
this run began directly at Phase D. Eight stories ran across three phases
respecting the file-sharing dependency graph in STORIES.md: wave 1 (S1, S5,
S6, S7 — fully independent, launched in parallel: S1/S5 as native Claude
subagents, S6/S7 as opencode background runs on glm/deepseek respectively);
wave 2 (S2 alone, since it shares `preflight.py` with S1 and had to build on
S1's landed tree); wave 3 (S3 ∥ S4, independent of each other, both needing
S2's landed scenario-check shape); and S8 (this document plus live
verification), done directly by the orchestrating session rather than
delegated. Every opencode connectivity check passed on the first try, every
agent (native and opencode alike) produced passing code on its first
attempt — no restarts were needed during Phase D.

Phase E ran in a later turn, once the user confirmed local Docker was up and
explicitly authorized a real engine start and real mod downloads for e2e
purposes. Rather than synthetic fixtures, the local dev DB's one server
definition was used as a live testbed: its mods and scenario id were
deliberately set to reproduce the sprint's own trigger incident (a
mod-GUID-guessed scenario id, an old-declared-version addon), and a real
server start plus a real `mod_sync` against the live Workshop API confirmed
every one of F1–F5 end to end. That live pass surfaced two real gaps: one
in-scope (S6's `source` field computed but never wired into the API
response — fixed, with new route-level regression tests) and one
out-of-scope pre-existing bug from the day before this sprint (`mod_sync`
crashing on any rescan of an unchanged scenario — fixed, with a regression
test confirmed against the pre-fix code via `git stash`). Both fixes were
re-verified against the full test suite (396/396) and against the live
container after a rebuild.
