# Reforger Manager — Sprint 6 stories

> Context + facts live in [PLAN.md](PLAN.md). `fact #n` = its *Code-level facts*;
> `§n` = its *Feature set*. Line numbers verified 2026-09-04 — trust symbol names
> if one drifted.

## Shape

Five stories, **fully parallel** — file sets are disjoint:

| Story | Agent | Files |
|---|---|---|
| S1 | native Claude (Sonnet 5) | `backend/app/mods/downloader.py`, `backend/app/main.py`, new `backend/tests/test_download_closure.py` |
| S2 | opencode · deepseek | `backend/app/mods/updates.py`, `backend/app/mods/freespace.py`, `backend/app/api/mods.py`, `backend/app/api/servers.py`, `backend/tests/test_metadata_selfheal.py` (new) |
| S3 | opencode · glm | `backend/app/mods/verify.py`, extend `backend/tests/test_verify.py` |
| S5 | opencode · glm | `frontend/src/components/server/ConsolePanel.tsx` |
| S6 | opencode · deepseek | `frontend/src/components/server/ServerForm.tsx` |
| S4 | native Claude | integration `pytest -q` + `npm run build` + `.sprints/6/RESULTS.md` (after S1–S3, S5, S6 merge) |

Every story: green `cd backend && ./.venv/Scripts/python.exe -m pytest -q`
(S1–S3) or `cd frontend && npm run build` (S5, S6) before hand-off.
Don't touch `.sprints/`, models, migrations.

---

## S1 — download closure verification · PLAN §1, facts 1-5, 9

**Deliver.**

1. `downloader.py`: pure helper `partition_present(expected: set[str]) -> tuple[set[str], set[str]]` —
   runs `scan_all()`, returns `(present, missing)` by addon-dir existence.
2. Closure resolution: `expected_closure(guids) -> set[str]` — wrap
   `resolve_dependencies` (fact 5) in try/except; on **any** exception return
   `set(guids)` and `await ctx.log("closure unresolved, verifying requested set only: ...")`.
3. `main.py` `_job_mod_download` (fact 2): after `run_mod_download` —
   - `expected = await expected_closure(guids)`; `present, _ = partition_present(expected)`;
     `missing = expected - present`.
   - if `missing`: `await ctx.log(f"closure incomplete, retrying {len(missing)}: ...")`,
     re-run the engine **once** for the missing subset (same code path as
     `run_mod_download`), rescan.
   - if still missing: `raise RuntimeError(f"download incomplete; missing on disk: {sorted(missing)}")`
     → job fails with the list in `current_step`/log (job manager handles it).
   - `result["missing_after_retry"] = sorted(still_missing)` (empty on success — fact 4).
   - `refresh_local_mods` call stays where it is; run it on the union.
4. Tests (`test_download_closure.py`, mirror patterns in `tests/test_verify.py`):
   - missing detection: tmpdir with 1 of 2 addon dirs → `(present={a}, missing={b})`.
   - closure degradation: resolver raising → expected == requested, warning logged.
   - job-level success: stub `_run_engine`; first pass missing 1, retry present → job succeeds, `missing_after_retry == []`.
   - job-level failure: both passes missing → job failed, missing list present.
   Stub `_run_engine` via monkeypatch; build fake addon dirs with a minimal `meta`
   file (copy the fixture trick from `tests/test_sync*.py` if one exists).

**Done when.** `pytest -q` green incl. the 4 new tests; existing downloader tests untouched and green.

**Watch out.**
- Empty `downloaded` + nothing missing must **succeed** (idempotent re-run) — decision 3.
- One retry only (decision 2); never loop. Keep total work inside the watchdog budget (fact 9).
- Don't change `_finish_ok`'s returned keys.

---

## S2 — metadata self-heal · PLAN §2, facts 6-7

**Deliver.**

1. `updates.py`: in the update-check loop (117-146), backfill size from the
   payload already in hand — `mod.get("size")` falling back to
   `versions[0]["size"]` (chain per fact 7); write only when `mod.size` is None.
2. `freespace.py`: `async def ensure_sizes(session, mods: list[Mod]) -> None` —
   for each row with `size is None`: `enrich_one(session, m.guid, client=workshop)`
   (sync.py:134-145 does the write); swallow/log per-row Workshop failures.
3. Wire-up: download route (api/mods.py:277-278) and `guard_update_scope`
   (freespace.py:56-75 — make it `async`; callers api/mods.py:226 +
   api/servers.py:47 are async routes) call `ensure_sizes` **before**
   `estimate_download_bytes`. Still-NULL → existing 409, message text unchanged.
4. Tests (`test_metadata_selfheal.py`):
   - `_record_workshop_state` (or its caller) writes size when payload has it; leaves existing size alone.
   - download route: row with `size=None` + stubbed workshop client returning a size → 202, row enriched.
   - 409 persists when the enrich client raises.
   - Patch the workshop client like existing mod-API tests do (find the fixture in
     `tests/` — search `workshop` usage); never hit the network.

**Done when.** `pytest -q` green incl. the 3 new tests; update-check + download
route + both guard callers pass.

**Watch out.**
- Exactly one enrich attempt per request (decision 5). No loops, no retries.
- Don't rename the 409 message; don't touch `check_free_space`'s signature
  (sync, pure) — healing lives in the async wrapper.
- `guard_update_scope` going async changes a call signature — grep for its callers,
  update all, keep behaviour identical for all-local sets.

---

## S3 — verify_repair console.log tail · PLAN §3, fact 8

**Deliver.**

1. `verify.py`: create a temp dir; add `-profile <tmp>/profile` to `_build_args()`;
   read lines by tailing the newest `logs/logs_*/console.log` under it (copy the
   wait-for-log + tail loop from downloader.py:221-237, incl. the 120 s
   appear-timeout). Remove the `stdout=PIPE` read; keep `stderr=STDOUT → DEVNULL`
   equivalent (engine logs to console.log, not stdout).
2. Feed every console.log line through the existing `_VerifyProgress.consume`
   and `ctx.log` pipeline — counters/regexes untouched.
3. Keep: child kill on cancel (173-179), overall timeout, final result shape.
4. Tests (extend `test_verify.py`): keep the synthetic-line tests green; add one
   test that the line source is read from a fake console.log file (pre-seed the
   tmp profile with a log containing verified/repaired lines) and counters tick.

**Done when.** `pytest -q` green; verify progress no longer depends on stdout.

**Watch out.**
- console.log appears asynchronously — reuse the downloader's appear-timeout
  pattern, don't poll forever.
- Don't change `_build_args` flags other than adding `-profile`.
- The engine writes `logs/logs_<ts>/`; resolve "newest" by name sort.

---

## S5 — Console tab autoscroll · PLAN §4, decision 7

**Deliver** (`frontend/src/components/server/ConsolePanel.tsx` only).

1. `const [autoScroll, setAutoScroll] = useState(true)`; checkbox in the
   controls row (next to the "Hide known spam" label, same styling):
   `Autoscroll`.
2. `const preRef = useRef<HTMLPreElement>(null)` on the `<pre>` (line ~91).
3. `useEffect` keyed on `displayed.length`: when `autoScroll`,
   `preRef.current?.scrollTo({ top: preRef.current.scrollHeight })`.
4. While unchecked, the `<pre>` keeps manual scroll position — do **not**
   scroll at all on new lines.

**Done when.** `cd frontend && npm run build` clean; manual read-through:
new lines arrive → view pinned to bottom when checked; scrolling up with the
box checked still snaps back (accepted), box unchecked → stays put.

**Watch out.**
- `displayed` is recomputed every render — depend on `displayed.length`, not
  the array identity, or the effect fires every render.
- Keep the websocket/search/hold-spam logic untouched; this is a view-only change.

---

## S6 — bind_address defaults empty · PLAN §5, decision 8

**Deliver** (`frontend/src/components/server/ServerForm.tsx` only).

1. Line ~120: `bind_address: "0.0.0.0"` → `bind_address: ""` (matches the
   `public_address` empty default above it).
2. Line ~789-794: add `placeholder="empty = default interface (same as public_address)"`.
3. Nothing else: the field already trims (~437) and diffs before patching
   (~458); backend `ServerUpdate.bind_address: str | None` accepts `""` and
   `config_gen.py:71` emits `"bindAddress": ""`. Existing servers keep their
   stored value in edit mode (loaded at ~149).

**Done when.** `cd frontend && npm run build` clean; new-server form shows an
empty bind_address; saving empty persists `""`.

**Watch out.**
- Do NOT change backend model/schema defaults or the migration
  (`0.0.0.0` server_default stays) — this story is the form default only.
- Don't touch `bind_port`/`public_*`/`a2s_*` defaults while in the file.

---

## S4 — integration + RESULTS.md (native, thin, after S1–S3, S5, S6)

1. `cd backend && ./.venv/Scripts/python.exe -m pytest -q` — full suite green.
2. `cd frontend && npm run build` — clean.
3. Cross-story consistency read: S1's job-result keys additive; S2's 409 text
   unchanged; S3's regexes untouched; S5/S6 view-only. Fix trivial collisions only.
4. Write `.sprints/6/RESULTS.md` (shape of `.sprints/5/RESULTS.md`): status,
   story table (S1–S3, S5, S6 + agents), incident-chain → fix mapping (F1–F5;
   F1 *mitigated* by S1 only — full orphan tracking remains a non-goal), UI
   changes (autoscroll, bind_address default), tests added, "not exercised"
   list (Docker, live stack, real Workshop, browser), date 2026-09-04.
