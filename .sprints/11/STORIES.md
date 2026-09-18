# Reforger Manager — Sprint 11 stories

> Context, evidence and code facts: [PLAN.md](PLAN.md). Trust symbol names over line numbers.

## Shared reference (read before starting any story)

| Thing | Where | Notes |
|---|---|---|
| Pre-flight | `backend/app/servers/preflight.py` — `preflight()` `:58-224`, `_version_compat()` `:322-338`, `_verdict()` `:344-346` | Add scenario check here (S2). Fix false positives first (S1). |
| Builtin GUIDs | `backend/app/mods/resolve.py:40-43` — `ENGINE_BUILTIN_GUIDS` | `58D0FB3206B6F859`, `5614BBCCBB55ED1C`. Pre-flight never imports it. |
| Scenario rows | `backend/app/models/mod.py:101-116` — `mod_scenarios` | `name IS NOT NULL` = API-verified (proxy). Scanner id is a guess (`scanner.py:24-31`). |
| Scenario API | `backend/app/mods/workshop.py:307-311` — `get_scenarios()` | `GET /mods/{id}/scenarios`. Returns true resource GUIDs. |
| Sync enrich | `backend/app/mods/sync.py:212-253` — `_upsert_api_scenarios` | S3 repair pass hooks in after this. Never auto-rewrite `scenario_game_id`. |
| Downloader core | `backend/app/mods/downloader.py:372-380` — `run_mod_download(ctx, guids, versions)` | Already list-shaped. S5 is API + MCP plumbing only. Guard `:243-245` stays. |
| Download route | `backend/app/api/mods.py:363-379` — `download_mod(guid)` | Single-GUID. Keep as wrapper over batch route. |
| Start path | `backend/app/api/servers.py:596-603` — `start_server`; `backend/app/servers/supervisor.py:start()` | S4 surfaces pre-flight findings in response. Still starts. |
| Log view | `backend/app/mods/logview.py:99-111` — `current_log()` / `resolve` | Hardcodes `logs/console.log`. S6 prefers `logs/logs_*/console.log`. |
| Server update | `backend/app/api/servers.py:256-267` — `update_server` | Returns full `Server` incl. `mods`. S7 trims it. |
| Mods list | `backend/app/api/mods.py:186-231` — `list_mods` | S7 adds compact mode. |
| Config preview | `backend/app/mcp/tools.py` — `get_server_config` | Already exists. No diff tool (non-goal). |
| Test harness | `backend/tests/test_refresh_local_mods.py:38-47`, any route test | Self-contained. No `conftest.py`. |
| Frontend gate | `npm run build` | Only frontend check. No JS tests. |

Verify: `cd backend && .venv/Scripts/python.exe -m pytest -q` · `cd frontend && npm run build`

## Shape

| Story | Agent | Files | Depends on |
|---|---|---|---|
| S1 pre-flight: `blocked` means blocked | claude | `servers/preflight.py`, `tests/test_preflight_blocked.py` | — |
| S2 scenario check in pre-flight | deepseek | `servers/preflight.py`, `tests/test_preflight_scenario.py` | S1 |
| S3 sync repair warning | glm | `mods/sync.py`, `tests/test_sync_scenario_repair.py` | S2 |
| S4 `start_server` surfaces findings | deepseek | `api/servers.py`, `servers/supervisor.py`, test | S1, S2 |
| S5 batch downloads | claude | `api/mods.py`, `mcp/tools.py`, test | — |
| S6 `tail_log` reads live session log | glm | `mods/logview.py`, test | — |
| S7 trim responses | deepseek | `api/servers.py`, `api/mods.py`, `mcp/tools.py` | — |
| S8 fleet check + RESULTS.md | claude | `RESULTS.md` | all |

**Parallelism.** S1 ∥ S5 ∥ S6 ∥ S7 start together. Then S2 (needs S1, same file). Then S3 ∥ S4 (need S2's shapes). S8 last.
**Serialise:** S1 → S2 (shared `preflight.py`). Nothing else shares a file.

---

## S1 — pre-flight: `blocked` means blocked

**Deliver.**

1. Import `ENGINE_BUILTIN_GUIDS` from `..mods.resolve`. Skip those GUIDs entirely in the `for guid, node in nodes.items()` loop — no Workshop lookup, no checks, no `resolved_mods` entry.
2. Downgrade `_version_compat` older-than-engine from `blocked` to `warn`. Keep existing `warn` copy. Keep `blocked` for addon newer than engine.
3. Leave unlisted/private/obsolete logic as is.

**Done when.** PLAN tests 1–3 pass: builtin GUID emits no check and no `blocked`; older addon → `warn`; newer addon → `blocked`; warnings-only server → `verdict: "warn"`. Exactly that file.

**Watch out.** Do not delete the verdict. Do not touch scenario logic (S2).

---

## S2 — scenario check in pre-flight

Read `server.scenario_game_id`. Resolve against `mod_scenarios` rows of the server's enabled mods.

**Deliver.** One new check producer, `warn` only, never `blocked`:

1. Verified match (`name IS NOT NULL`) → no check.
2. Guess match (`name IS NULL`) → `warn`, ask for a mod sync.
3. No GUID match but path portion matches a verified row under a different GUID → `warn` **naming the verified id** (the server-32 shape).
4. No match at all → `warn` (mod disabled or absent).

**Done when.** PLAN tests 4–5 pass, incl. the server-32 seed (`{6148289172F86E7A}…` → names `{02778C5E44407C03}…`). No `blocked` in any case.

**Watch out.** Depends on S1 — build on its tree. Optional `source` column: take it only if cheap, else keep the `name IS NOT NULL` proxy.

---

## S3 — sync repair warning

**Deliver.** In `mod_sync`, after API enrichment: for each `Server` whose `scenario_game_id` has no verified match but whose path portion matches exactly one verified row, log at WARNING with both ids. Do not rewrite the column.

**Done when.** `pytest -q` green: wrong-GUID id logs both ids once; correct id logs nothing; ambiguous (2+ path matches) logs nothing or a generic warning, never a guess.

**Watch out.** Not a job. No auto-correct — silent mission changes caused this incident.

---

## S4 — `start_server` surfaces blocking findings

**Deliver.** `start_server` (route or `supervisor.start`) runs `preflight()` and includes blocking findings in its success response (e.g. `preflight_blocked: [...]`). It still starts. No refusal logic.

**Done when.** `pytest -q` green: start on a `blocked` server returns success + findings; start on a clean server returns empty list. Existing start tests still pass.

**Watch out.** Do not make the supervisor refuse (non-goal). Keep it a surface, not a gate.

---

## S5 — batch mod downloads

**Deliver.**

1. New route `POST /mods/download` taking `{guids: list[str], versions?: dict}`. Calls `run_mod_download` once. Upper-cases GUIDs, reuses free-space guard.
2. Keep `POST /mods/{guid}/download` as thin wrapper over the same path.
3. New MCP tool mirroring the batch route. Keep single-GUID tool as wrapper.

**Done when.** PLAN test 6 passes: one `run_mod_download` call for N guids; wrapper yields same job shape. Guard untouched.

**Watch out.** Job layer needs no change. Progress already aggregates. Do not relax the running-server guard.

---

## S6 — `tail_log` reads the live session log

**Deliver.** In `logview.py` resolve: prefer newest `logs/logs_*/console.log` by dir mtime when any session dir exists; fall back to `logs/console.log` only when none exists. No merging. Return chosen file in response metadata. Keep ancestry checks under profile dir.

**Done when.** PLAN test 7 passes: prefers session log, falls back with no session dir, refuses path escape.

**Watch out.** Fixes stale content + torn lines. Do not merge the two files.

---

## S7 — trim mutation and listing responses

**Deliver.**

1. `update_server` (and sibling mutations if trivial) omit `mods` by default, return `mod_count`; add `?include=mods` to restore it. `get_server` keeps full shape.
2. `list_mods` gains `?compact=true` (drops `required_by`, `versions`, `thumbnail`). Default the MCP `list_mods` tool to compact.

**Done when.** PLAN test 8 passes plus compact-list case. Existing callers unbroken.

**Watch out.** Ergonomics only. No behaviour change. Frontend asks for what it needs.

---

## S8 — fleet check, gates, RESULTS.md

**Deliver.**

1. Full gates: `pytest -q` + `npm run build`.
2. `docker compose up -d --build`, then PLAN manual matrix: pre-flight on servers 13/22/24/32 shows no `blocked`; scratch server with mod-GUID scenario id names the correction; batch download spawns one engine; `tail_log` follows the live session across restart.
3. `.sprints/11/RESULTS.md`: status, story table, data-model changes, verification performed, explicit **not exercised** list, observations, agent rotation paragraph.

**Watch out.** Check which server runs before any destructive step. Never touch production TrueNAS/SSH.
