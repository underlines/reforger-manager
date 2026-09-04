# Reforger Manager — Sprint 6 plan

## Context

2026-09-04 incident on the live stack: the manager container was recreated
mid-download (00:37:54 UTC). Five compounding gaps turned a routine redeploy
into ~45 min of manual forensics:

| # | Failure | Code |
|---|---|---|
| F1 | Container restart killed the headless engine mid-download → half-written addon dirs; DB rows never updated | no child-process tracking (core/jobs.py:106 only fails orphan *rows*) |
| F2 | Re-run skipped the half-written dirs; job "succeeded" with `downloaded: {}` — success is never verified on disk | downloader.py:269-284, core/jobs.py:275-293 |
| F3 | `refreshed_local` never diffed vs requested; the needed-filter that does exactly this exists unused | downloader.py:380-408 (`ensure_mods_ready`, zero callers) |
| F4 | Free-space guard hard-409s on NULL `mods.size`; update-check refreshes rows but never writes `size` | freespace.py:22-53, updates.py:235-245 |
| F5 | verify_repair reads block-buffered stdout (anti-pattern the downloader itself documents at downloader.py:221) → progress stuck at "checked 0", false "stuck" reading | verify.py:143-167 |

## Goal

`succeeded` on a download job means the closure is on disk. NULL-size rows
self-heal on the paths that touch them. verify progress is honest. No schema
changes, no new routes.

## Decisions locked

| # | Decision |
|---|---|
| 1 | Backend verification = `cd backend && ./.venv/Scripts/python.exe -m pytest -q`. Frontend = `cd frontend && npm run build`. No Docker, no live API. |
| 2 | S1 retries missing mods **once**, then fails the job with the missing list. Closure resolution degrades to "requested set only" when Workshop errors (503) — resolve failures log a warning, never fail the job. |
| 3 | Empty-`downloaded` is legitimate when nothing is missing (everything already local). Only missing-on-disk fails. |
| 4 | Job result changes are additive keys only (`missing_after_retry`); never remove/repurpose existing keys. |
| 5 | Guard self-heal does exactly **one** enrich pass per request — no background loops. 409 message text stays compatible (agents/tests match on it). |
| 6 | No migrations, no model changes, no MCP edits, no commit/push. |
| 7 | Console autoscroll: checkbox default **on**; only auto-scroll when the user is (near) the bottom or the box is checked — checked box = always follow tail. |
| 8 | `bind_address` form default becomes `""` (same empty default as `public_address`); backend untouched — `config_gen.py:71` already writes `"bindAddress": server.bind_address or ""`. Existing rows keep their stored value. |

## Code-level facts (verified 2026-09-04)

1. Job success = coroutine returned without raising; no disk check (core/jobs.py:275-293).
2. `_job_mod_download` (main.py:190-207) post-processes `run_mod_download` with
   `refresh_local_mods(guids)` → `result["refreshed_local"]`; refresh failure is
   swallowed into a log line; never diffed against `guids`.
3. `_run_engine` (downloader.py:240-284): spawns engine, stdout DEVNULL, tails
   `<profile>/logs/logs_*/console.log`; `_finish_ok` returns
   `downloaded = parser.names` — only GUIDs that emitted "Addon Download started".
4. `ensure_mods_ready` (downloader.py:380-408): `needed = row missing OR not
   is_local OR pinned-version mismatch`. Exported, unused in app code.
5. Closure: `resolve_dependencies` BFS (resolve.py:114-239) — unresolved nodes
   reported, doesn't raise. `scan_all` (scanner.py:282-306) is cheap, returns
   per-dir meta incl. size; `refresh_local_mods` wraps it (sync.py:292-312).
6. `check_free_space` (freespace.py:22-53) raises 409 when any affected
   `mods.size` is NULL; `guard_update_scope` (freespace.py:56-75) same. Callers:
   api/mods.py:226,277-278, api/servers.py:47 (import).
7. `_record_workshop_state` (updates.py:235-245) writes name/version/api_state,
   **not** `size` — although the payload it already holds has it
   (workshop.py:284-299; `enrich_one` sync.py:134-145 shows the fallback chain
   `mod["size"] → versions[0]["size"]`).
8. verify.py runs the engine with `stdout=PIPE` (verify.py:143-151) and counts
   progress only from `_VERIFIED_RE/_REPAIRED_RE/_FAILED_RE` lines (21-35,
   54-65). The proven alternative line source is the downloader's console.log
   tail (downloader.py:221-237). Child kill on cancel already works (verify.py:173-179).
9. Watchdog auto-cancels jobs past `job_max_runtime_seconds` (default 3600,
   config.py:76) — keep per-attempt work well inside it.

## Feature set

### 1. S1 — post-download closure verification (F1–F3)
New helper(s) in `downloader.py`; orchestration in `_job_mod_download`:

```
expected = guids ∪ closure(guids)     # try/except → guids, log warning
present  = {d.guid for d in scan_all()}
missing  = expected - present
if missing: re-run engine once for missing; rescan
if still missing: raise → job failed, result carries missing list
```

Reuse `ensure_mods_ready`'s needed-filter semantics. Result keys added:
`missing_after_retry`. `downloaded` semantics unchanged.

### 2. S2 — metadata self-heal (F4)
- updates.py: backfill `mod.size` in `_record_workshop_state`-adjacent code
  from the already-fetched payload (no new Workshop calls).
- freespace.py: async `ensure_sizes(session, mods)` — `enrich_one` each
  NULL-size row once, re-estimate; wire into the download route and
  `guard_update_scope` (make it async; both callers are async routes).
  409 only if still NULL after the heal.

### 3. S3 — verify_repair honest progress (F5)
verify.py: pass a temp `-profile` dir (mirror the downloader), tail its
`logs/logs_*/console.log` instead of the stdout pipe. `_VerifyProgress`
regexes/API unchanged — only the line source changes.

### 4. S5 — Console tab autoscroll (frontend, UI-1)
`frontend/src/components/server/ConsolePanel.tsx`: `autoScroll` state
(default true), checkbox in the controls row next to "Hide known spam",
ref on the `<pre>`, effect pinning `scrollTop = scrollHeight` on new lines
while enabled.

### 5. S6 — bind_address defaults empty (frontend, UI-2)
`frontend/src/components/server/ServerForm.tsx`: form default
`bind_address: ""` (line ~120, was `"0.0.0.0"`), placeholder
"empty = default interface (like public_address)". Field submit already
trims (line ~437) and only patches on change (line ~458). Backend untouched.

## Data-model changes
None.

## Non-goals
- Child-process pid tracking / orphan reaping on startup; auto re-queue after restart.
- Library-wide drift sweeper beyond S1's scoped check.
- MCP tools, backend routes beyond this sprint's files, docs beyond this sprint's files.

## Verification

```bash
cd backend && ./.venv/Scripts/python.exe -m pytest -q
cd frontend && npm run build
```
Green, with each story's new tests visibly collected / build clean.
