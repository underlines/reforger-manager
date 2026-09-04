# Reforger Manager — Sprint 6 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-04 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: done, green

All stories implemented: S1 (download closure verification), S2 (metadata
self-heal), S3 (verify console.log tail), S5 (Console autoscroll), S6
(bind_address default). Backend suite green (**296 passed**, sprint-5 baseline
288 → +8 for this sprint); frontend `npm run build` clean. No Docker, no live
stack.

## What shipped

- **S1 — download closure verification.** `downloader.py` gains
  `partition_present(expected)` (disk truth via `scan_all`, only dirs with a
  `meta` file count) and `expected_closure(ctx, guids)` (BFS via
  `resolve_dependencies`; any resolver failure degrades to the requested set +
  warning — never fails the job). `_job_mod_download` now diffs expected vs
  on-disk, retries missing mods **once**, and fails the job with
  `download incomplete; missing on disk: [...]` if still incomplete. Additive
  result key `missing_after_retry` (empty on success); existing keys untouched.
- **S2 — metadata self-heal.** `updates.py` backfills `mods.size` from the
  Workshop payload it already fetches (only when NULL, `mod["size"]` →
  `versions[0]["size"]`). `freespace.py` gains async `ensure_sizes(...)` — one
  `enrich_one` attempt per NULL-size row, per-row failures swallowed — wired
  into the download route and into `guard_update_scope` (now async; both
  callers awaited). The 409 "no recorded size" message is unchanged; it is now
  only reachable when the Workshop can't provide a size at all.
- **S3 — verify_repair honest progress.** `verify.py` spawns the engine with a
  temp `-profile` dir and `stdout=DEVNULL` (no more block-buffered PIPE), waits
  for the newest `logs/logs_*/console.log` (120 s appear timeout, fail-fast if
  the engine exits before logging) and tails it into the unchanged
  `_VerifyProgress` pipeline. Cancel/kill preserved (and now checked per poll
  tick, so a silent engine is cancellable). Regexes/step format/result shape
  byte-for-byte unchanged.
- **S5 — Console autoscroll.** `ConsolePanel.tsx`: `Autoscroll` checkbox
  (default on) next to "Hide known spam"; `preRef` + effect pins the view to
  the bottom on `displayed.length` change while enabled; unchecked leaves
  manual scroll position alone.
- **S6 — bind_address defaults empty.** `ServerForm.tsx` form default
  `""` (matches `public_address`) + placeholder hint. Backend untouched
  (`config_gen` already emits `"bindAddress": ""` for empty); existing servers
  keep their stored value.

## Story-by-story

| Story | Implementer | Delivered |
| --- | --- | --- |
| S1 | internal agent (Claude) | closure verify + one retry + fail-with-missing-list, 4 tests |
| S2 | opencode · deepseek | size backfill in update-check + `ensure_sizes` guard self-heal, 3 tests |
| S3 | internal agent (Claude) | console.log tail for verify (after two glm stalls), 1 test |
| S5 | opencode · glm | Console autoscroll checkbox |
| S6 | opencode · deepseek | bind_address empty default |

## Data-model changes

None. All fixes are behavioural over existing columns; no migrations.

## Incident-chain → fix mapping

| Failure (2026-09-04 incident) | Fix |
| --- | --- |
| F1 container restart killed engine mid-download → half-written dirs | *mitigated* by S1 (detects + retries them); full orphan-process tracking remains a non-goal |
| F2 job "succeeded" with `downloaded: {}` | S1: success now requires the closure on disk |
| F3 `refreshed_local` never diffed vs requested | S1: explicit expected-vs-present diff |
| F4 free-space guard deadlocked on NULL sizes | S2: update-check backfill + one-shot guard self-heal |
| F5 verify progress stuck at "checked 0" | S3: console.log tail instead of stdout pipe |

## Verification performed

- `cd backend && ./.venv/Scripts/python.exe -m pytest -q` → **296 passed,
  94 warnings in 50.33s** (full suite, sqlite+aiosqlite).
- `cd frontend && npm run build` → clean (`tsc -b && vite build`,
  pre-existing >500 kB chunk warning only).
- New tests: 4 (`test_download_closure.py`) + 3 (`test_metadata_selfheal.py`)
  + 1 (`test_verify.py::test_job_follows_engine_console_log`).

## Not exercised

- Docker / live stack / real Workshop API (all stubbed in tests).
- Real engine download/verify runs (S1/S3 tests monkeypatch the spawn).
- Browser-level autoscroll feel (view-only change, build-gated).
- CI / deploy; nothing committed or pushed.

## How this sprint was implemented

Orchestrator (Claude) ran four parallel implementation tracks: internal
subagents for S1 and S3, `opencode run` agents (deepseek-v4-flash,
glm-5.3-flash) for S2/S5/S6 per the sprint skill's rotation. glm stalled twice
on S3 (exited with zero writes, then hung in mock-internals deliberation) —
reassigned to an internal agent, which landed it first try and also cleaned a
concurrent duplicate-test fragment the killed agent had left in
`test_verify.py`. S2 initially had 1 failing test (route-session commit
semantics), fixed by the agent to 295 green; S1 reported 2 transient failures
caused by S2's in-flight edits on the shared tree, resolved once S2 settled.
Final integration run (S4): full pytest + npm build green by the orchestrator.
