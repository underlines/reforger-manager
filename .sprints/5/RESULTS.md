# Reforger Manager — Sprint 5 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-04 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: done, green

All three stories (S1–S3) implemented. Backend suite green (**288 passed**,
baseline 282 → +6 for this sprint); no frontend work, no Docker.

## What shipped

- **6 new MCP tools** in the `ToolSpec` table (`backend/app/mcp/tools.py`) —
  four library-wide mod tools in the `# Mods` group, `delete_mod_local` right
  after `delete_mod`, and `create_modpack_from_server` in the `# Modpacks`
  group immediately after `apply_modpack`. One `ToolSpec` tuple entry each —
  no business logic, 1:1 with the REST routes.
- **Test-set plumbing** (`backend/tests/test_mcp_tools_mutations.py`) — the
  six names added to `MUTATION_TOOLS`; the four job-returning names added to
  `JOB_RETURNING_TOOLS`; 6 new tests: 4 in `JobReturningToolTests`
  (each asserting the verbatim `{job_id: 7, kind: ...}` `JobEnqueuedOut`) and
  2 in `ConfirmGateTests` (`delete_mod_local` and `create_modpack_from_server`,
  each covering the no-confirm gate plus a confirm call that reaches the API).
- **No README change needed** — "Connect an agent" describes the surface
  generically ("the REST verbs mirrored 1:1 plus three read-only composites")
  and does not enumerate individual tool names, so there was no list to extend.

## Story-by-story

| Story | Implementer | Delivered |
| --- | --- | --- |
| S1 | native Claude · Sonnet 5 | `download_mod`, `verify_mods`, `check_all_mod_updates`, `apply_all_mod_updates` + shared plumbing (import line, MUTATION_TOOLS / JOB_RETURNING_TOOLS) + 4 job-enqueue tests |
| S2 | opencode · deepseek | `delete_mod_local` + 1 confirm-gate test (seeds `Mod(is_local=False)`; the confirm call reaches the API and hits the 409 "no on-disk files" path) |
| S3 | opencode · glm | `create_modpack_from_server` + 1 confirm-gate test (seeds a `ServerMod`, asserts `pins_note` is null when the source has no pins) + README check + RESULTS.md |

## Data-model changes

None. All six tools mirror existing REST endpoints over existing models and
schemas; `create_modpack_from_server` reuses the API's own
`ModpackFromServerIn` / `ModpackFromServerOut` schemas via the existing
`from ..schemas.modpack import ...` line.

## New MCP tool surface (this sprint)

| Tool | Method + path | confirm | Returns |
| --- | --- | --- | --- |
| `download_mod` | `POST /api/mods/{guid}/download` | yes | job `mod_download` |
| `verify_mods` | `POST /api/mods/verify` | yes | job `verify_repair` |
| `check_all_mod_updates` | `POST /api/mods/updates/check` | yes | job `mod_update_check` |
| `apply_all_mod_updates` | `POST /api/mods/updates/apply` | yes | job `mod_update_apply` |
| `delete_mod_local` | `DELETE /api/mods/{guid}/local` | yes | returns `ModOut` |
| `create_modpack_from_server` | `POST /api/modpacks/from-server/{server_id}` | yes | returns `ModpackFromServerOut` |

All four job-returning tools document `wait_for_job(job_id)` in their
descriptions (asserted by `test_job_tool_descriptions_document_wait_for_job`);
all six pass the >80-char side-effect assertion of
`MutationToolSurfaceTests`.

## Verification performed

- `cd backend && ./.venv/Scripts/python.exe -m pytest -q` → **288 passed,
  94 warnings in 49.68s** (full suite, sqlite+aiosqlite, no Postgres).
- `./.venv/Scripts/python.exe -m pytest -q tests/test_mcp_tools_mutations.py`
  → **36 passed in 9.90s**, including the new
  `test_create_modpack_from_server_gates_on_confirm_and_creates_via_the_api`
  and `MutationToolSurfaceTests` with `create_modpack_from_server` in
  `MUTATION_TOOLS`.
- No Docker, no live `/mcp` smoke.

## Not exercised

- Docker / live `/mcp` smoke (harness is the sqlite ASGI-transport one).
- Job execution for any of the four job-returning tools — the sqlite harness
  registers no job handlers, so only `JobEnqueuedOut` (`enqueue` mocked to 7)
  is asserted.
- `delete_mod_local` success path — needs a real addon directory on disk;
  the test exercises the no-confirm gate plus the 409 no-files branch.
- `download_mod` free-space-trip path (400/409 from the free-space guard).
- `verify_mods` with an explicit `guids` list — only the omit-guids
  (whole-library) call shape is tested.

## Minor observations

- README's "Connect an agent" section stays as-is: it describes the MCP
  surface generically ("the REST verbs mirrored 1:1 plus three read-only
  composites") and never lists tool names, so the six additions required no
  edit.
- `delete_mod_local` and `create_modpack_from_server` are body-returning, not
  job-returning — their descriptions deliberately omit `wait_for_job` and the
  spec has no `arg_model`/path quirk beyond the auto-typed `server_id: int`
  path param.

## How this sprint was implemented

Native Claude implemented S1 (the four job-returning library mod tools plus
the shared MUTATION_TOOLS/JOB_RETURNING_TOOLS plumbing and their four tests),
opencode/deepseek implemented S2 (`delete_mod_local` + its test), and
opencode/glm implemented S3 (`create_modpack_from_server` + its test, the
README check and this RESULTS.md). The three stories ran strictly sequentially
because all of them edit the same two files (`backend/app/mcp/tools.py` and
`backend/tests/test_mcp_tools_mutations.py`) and each story builds directly on
the previous story's rows in those files. No stalls or restarts were needed.
S1's first suite run had one failure — the `download_mod` test seeded a `Mod`
with no `size`, which the route's `check_free_space` guard rejects; adding
`size=1` fixed it. Every story was re-run green (`pytest -q`) by the
orchestrator before the next one started.