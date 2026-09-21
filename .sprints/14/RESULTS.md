# Reforger Manager — Sprint 14 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-21 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: complete

All 7 stories (S1–S7) implemented, each independently re-verified by the
orchestrator (not just trusted from the implementing agent's self-report)
against a green backend suite and/or a green frontend build. Two additional,
sprint-relevant issues were found during live browser verification and fixed
directly by the orchestrator, plus a follow-up pass closed out the remaining
"not exercised" gaps — see *Found and fixed during live verification*.

## Story-by-story

| # | Story | State | Agent | Notes |
|---|---|---|---|---|
| S1 | Relax the soft-delete guard | done | native (Sonnet 5) | `delete_local_mod`'s guard in `backend/app/api/mods.py` changed to `guid not in directly_referenced and guid in closure_owners`; `_delete_block_detail` gained a `dependency_only` flag so the 409 on this path names only the parent-dependency reason, never servers/modpacks. `delete_library_mod` untouched. Backend suite: 402/402. |
| S2 | Preflight reports local disk state | done | opencode · deepseek | One-line addition to `preflight()`: `detail["local"] = bool(mod is not None and mod.is_local)`. Extended `test_preflight.py` / `test_preflight_blocked.py`. 22/22 preflight tests green. |
| S3 | Unified delete/soft-delete UX | done | native (Sonnet 5) | `Mods.tsx` + `ModDetail.tsx`: renamed actions to "Delete downloaded files" / "Delete mod entirely" everywhere; both dialogs now show a live `GET /mods/{guid}/references` result in a prominent `Badge tone="bad"` block above the static copy; `ModDetail.tsx`'s delete error handling fixed to keep the dialog open (previously closed on failure); `ModDetail.tsx` gained the disk-only action it was missing. `npm run build` clean. |
| S4 | `ensure-ready` endpoint + shared enqueue helper | done | native (Sonnet 5) | Extracted `enqueue_download_job` (free-space guard → job enqueue) out of `mods.py`'s `_enqueue_mod_download` into `app/mods/downloader.py`, keeping the 404 loop in `mods.py`. New `POST /api/servers/{id}/mods/ensure-ready` in `servers.py`, loads via `_load`, diffs `resolved_mod_entries` against `Mod.is_local`/pinned version. New `ServerModsReadyOut` schema. Backend suite: 406/406. |
| S5 | Local-state badge + manual redownload | done | opencode · glm | New `frontend/src/components/mods/LocalStateBadge.tsx` (downloaded / not-cached / orphan states + polling "Redownload now"); wired into `ModsPanel.tsx`'s preflight render via a separate `guid → entry` map (left `preflightGroups` untouched, per its own documented limitation). `npm run build` clean. |
| S6 | Ensure-ready-then-start wiring | done | opencode · deepseek | New `frontend/src/lib/serverStart.ts` (`startServerReady`); wired into the three call sites the PLAN's grep identified: `ServerRow.tsx`, `SavesPanel.tsx` (×2). Correctly reused the existing `run(...)` busy-state key convention rather than inventing a new label. `npm run build` clean. |
| S7 | Surface local state on 3 more views | done (after 1 restart) | opencode · glm (stalled, 0 writes) → opencode · deepseek | First attempt (glm) exited 0 with no file changes — a documented opencode failure mode. Restarted with a blunter, anchor-heavy brief on deepseek; landed correctly in `Mods.tsx`, `ModDetail.tsx`, `Modpacks.tsx`, all reusing `LocalStateBadge` against already-available `is_local` data (`ModRecord`, `detail.is_local`, `ModGraphNode` respectively) — no backend widening was actually needed anywhere. `npm run build` clean. |

## Found and fixed during live verification

**A fourth, un-grepped `/start` call site.** PLAN fact #9 stated three call
sites were "grep-confirmed exhaustive." Live-clicking the server detail page's
own header **Start** button (`frontend/src/pages/ServerDetail.tsx:113-127`)
showed it firing `POST /api/servers/{id}/start` directly, bypassing
`ensure-ready` entirely — confirmed via the browser's network log and by
observing `is_local` stay `false` after a "successful" start. The literal
grep for `/start` missed it because the URL is built as a ternary
(`` `/api/servers/${id}/${server.is_running ? "stop" : "start"}` ``), so the
substring `/start` never appears in the source. Fixed directly (not
re-delegated, since it's the same one-line substitution S6 already applied
elsewhere): the `"start"` branch now calls `startServerReady(Number(id))`;
the `"stop"` branch is untouched. Rebuilt the Docker image and re-verified
live — see below. `npm run build` and the full backend suite stayed green.

**The "STILL REFERENCED" warning's trailing sentence overclaimed on the
disk-only path.** Both delete dialogs (S3) shared one static closing line,
*"The delete is refused while any of these hold a reference."* That's only
true for full-delete; after S1, disk-only succeeds whenever the mod is
directly assigned to a server/modpack, even if it also has `required_by`
entries — exactly the "Ronin AI" case used throughout this sprint's live
testing (assigned to server "test" **and** required by "CO-OP Conflict PVE").
Fixed in `Mods.tsx` and `ModDetail.tsx`: `renderReferences` now takes a
`"disk" | "full"` argument. Full-delete keeps the original sentence
unconditionally (still accurate — any reference blocks it). Disk-only shows
one of two sentences depending on `blocksDiskDelete`.
**First pass got this predicate wrong**: `required_by.length > 0` alone,
which would have called Ronin AI's own case "still blocks" — the opposite of
what actually happens, since being directly assigned overrides being someone's
dependency. Corrected to mirror the backend guard's actual shape:
`!servers.length && !modpacks.length && required_by.length > 0` (blocks only
when there is *no* direct assignment and the mod is purely a resolved
dependency). Re-verified live after the fix — see below.

## Data-model changes

None. `Mod.is_local` already existed and already carried the soft-delete-state
meaning, exactly as PLAN anticipated.

## New / changed API surface

New: `POST /api/servers/{server_id}/mods/ensure-ready` → `{job_id, kind, missing}`.

Changed: `DELETE /api/mods/{guid}/local` guard relaxed (blocks only a
pure-dependency mod, not a directly-assigned one) with an updated 409 message;
`GET /api/servers/{id}/preflight`'s `resolved_mods[]` entries gained `local: bool`.

New frontend: `lib/serverStart.ts` (`startServerReady`), `components/mods/LocalStateBadge.tsx`.

## Verification performed

- **Backend:** `backend/.venv/Scripts/python.exe -m pytest -q` → **406 passed**,
  independently re-run by the orchestrator after every story (not just taken
  on the implementing agent's word).
- **Frontend:** `npm run build` (`tsc -b && vite build`) clean after every
  story and after the ServerDetail.tsx fix.
- **Docker stack:** `docker compose up -d --build` (twice — once per-sprint,
  once again after the ServerDetail.tsx fix), clean startup logs, schema
  self-heal no-op (no data-model changes).
- **Live, via chrome-devtools MCP against `http://localhost:18090`:**
  - Soft-deleted "Ronin AI" (directly assigned to server "test" **and** a
    resolved dependency of "CO-OP Conflict PVE") from `/mods` — succeeded
    (previously would have 409'd), the dialog showed the live "STILL
    REFERENCED" warning naming both the server and the dependent mod, the row
    flipped to "NOT CACHED", and the `ServerMod` assignment survived (confirmed
    via the server's live config preview still listing it).
  - `ModsPanel.tsx`'s "Not cached locally" pre-flight section rendered the
    same mod with a "Redownload now" action.
  - Clicked the server's **Start** button: confirmed (after the fix above)
    `POST .../mods/ensure-ready` [202] → job enqueued → polled to
    `succeeded` → **then** `POST .../start` [200], server reached `RUNNING`.
    Confirmed via the API directly: `is_local` flipped back to `true`, job
    log shows `"local library refreshed for 1 addon(s): ['6294F6D5EDD5CA66']"`.
  - `ModDetail.tsx` for the same mod: confirmed the disk-only "Delete
    downloaded files" action now exists there too, with the identical live
    warning block as `/mods` (fixing the modpack-visibility gap fact #11
    described).
  - `Modpacks.tsx`: confirmed the pack-editor item row shows the same
    downloaded/redownload badge.
  - **Follow-up pass, after the copy fix above:** opened Ronin AI's disk-only
    dialog again — now reads *"None of these block deleting the downloaded
    files — only a dependency link would. The assignments above are kept, and
    this mod is refetched automatically the next time it's needed,"* correctly
    reflecting that this delete succeeds. Opened its full-delete dialog and
    clicked through: 409'd as `"mod 6294F6D5EDD5CA66 cannot be deleted — still
    referenced by server definition(s) test"`, dialog stayed open with the
    error inline (didn't close), full-delete's warning sentence unchanged and
    still accurate.
  - **Pinned-version redownload, exercised end-to-end via the API** (arbitrary
    version strings are accepted with no Workshop validation, so this doesn't
    need a mod with real multi-version history): pinned Ronin AI's server
    assignment to a synthetic `"1.0.26-pin-test"`, soft-deleted its files,
    called `ensure-ready` — the created job's `params.versions` carried
    `{"6294F6D5EDD5CA66": "1.0.26-pin-test"}`, **not** `"1.0.27"` (the actual
    latest/installed version). The download then failed for real, exactly as
    it should — that string isn't a real Workshop version — confirming the
    pin is honored faithfully rather than silently substituting latest on a
    bad pin. Cleaned up afterward: unpinned, redownloaded the real version,
    confirmed `is_local: true` and `pinned_version: null` restored.
- **Docker e2e (`scripts/e2e_live.py --phases api,engine,server,mods`),
  run inside the container per `scripts/README.md`:** **86 passed, 3 failed.**
  All 3 failures are pre-existing and unrelated to this sprint:
  - `favourite sorts first` — favourites feature, untouched by Sprint 14.
  - `dependency mod has a version history` — mod version-history feature,
    untouched by Sprint 14.
  - `POST /mods/updates/check -> 202 (got 405)` — this route was
    intentionally removed in Sprint 13 (`fa7e1f1`, "drop 3rd-party
    update-check API"); confirmed via `grep` that no such route exists
    anywhere in `backend/app`. The e2e script is stale here, predating
    Sprint 13; not a regression.
  Everything this sprint actually touches passed inside that run: server
  create/start/stop, the real `mod_download` job (`is_local` flip, on-disk
  size, orphan detection), preflight, and RCON/log/scheduled-restart
  (unaffected, confirms no regression from the `mods.py`/`servers.py` edits).

## Not exercised

- `SavesPanel.tsx`'s two `startServerReady` call sites (restart-with-save,
  restart-fresh) — code-reviewed and built clean, not live-clicked (no save
  point existed on the test server to arm one against).
- Production TrueNAS / SSH — out of scope per PLAN Non-goals; untouched.

## How this sprint was implemented

Delegated story-by-story per `AGENTS.md`'s sprint workflow, split across three
agent types as instructed (roughly half native Sonnet 5, half opencode):
native Sonnet 5 subagents took the three trickiest/foundational stories (S1's
guard-semantics change, S3's multi-file UX rewrite, S4's new endpoint +
extraction); opencode `deepseek-v4.1-flash` and `glm-5.3-flash` split the four
more mechanical/additive ones (S2, S5, S6, S7). One opencode run (S7 on glm)
exited cleanly but made zero file changes — a known opencode failure mode — and
was restarted on deepseek with a more directive, anchor-heavy brief that named
exact line numbers and pre-answered every judgment call; the retry landed
correctly. Every story was independently re-verified by the orchestrator
(pytest and/or `npm run build`, re-run fresh rather than trusting each agent's
self-reported result) before the next dependency phase started. Phase E's live
verification then surfaced one gap the sprint's own planning had missed (the
un-grepped fourth `/start` call site), which the orchestrator fixed directly
as a same-pattern, one-line change rather than spinning up another delegation
round.

A follow-up pass, requested after the first RESULTS.md draft, addressed the
one non-blocking copy issue found during live verification and closed out two
of the four "not exercised" gaps (full-delete refusal, pinned-version
redownload) with real live/API click-throughs rather than leaving them as
inferred-safe. Both were small enough (a two-file copy fix; two `curl` calls
plus a rebuild) that the orchestrator made them directly rather than
delegating. `SavesPanel.tsx`'s two call sites and TrueNAS remain out of scope,
per the cost/value call made when they were first flagged.
