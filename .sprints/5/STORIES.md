# Reforger Manager — Sprint 5 implementation stories

> Context, decisions and the full spec live in [PLAN.md](PLAN.md).
> `fact #n` = PLAN.md *Code-level facts*; `§n` = its *Feature set* number.
> Line numbers were verified at sprint start — if one has drifted, trust the
> symbol name.

## Shape of the sprint

Three stories, **strictly sequential** — S1 → S2 → S3. All three edit the same
two files (`backend/app/mcp/tools.py` and
`backend/tests/test_mcp_tools_mutations.py`), so there is **no parallelism**.
Each story must be fully green (`pytest -q`) before the next starts, and each
implementer works on the tree as the previous story left it.

| Story | Agent | Slice |
|---|---|---|
| S1 | native Claude (Sonnet 5) | the 4 job-returning mod tools + shared plumbing (imports, test sets) + 4 job tests |
| S2 | opencode · deepseek | `delete_mod_local` (body-returning) + 1 test |
| S3 | opencode · glm | `create_modpack_from_server` (body-returning) + 1 test + README check + RESULTS.md |

**Verification for every story:**
`cd backend && ./.venv/Scripts/python.exe -m pytest -q` — green, new tests collected.

---

## Shared reference (read before starting any story)

| Thing | Where | Notes |
|---|---|---|
| The tool table | `backend/app/mcp/tools.py:186` `TOOL_SPECS: tuple[ToolSpec, ...]` | Add rows here. `register_tools` (`:1042`) iterates it — no other wiring (fact #1). |
| `ToolSpec` shape | `tools.py:55-78` | `name, method, path, description, arg_model=None, query_model=None, multipart_field=None, confirm=False, timeout_s=60.0` (fact #2). |
| Existing mod mutation rows to copy the register from | `scan_mods` `tools.py:667`, `add_mod` `:680`, `delete_mod` `:694` | Job-returning row example: `scan_mods`. DELETE-returning-a-body example: `unpin_server_mod` `:654`. |
| `# Mods` group boundary | after `delete_mod` ends `tools.py:705`, before `# Modpacks` `:706` | S1 + S2 rows go here. |
| `# Modpacks` group | `apply_modpack` ends `tools.py:759` | S3 row goes right after it. |
| Confirm gate | `tools.py:977-983` — automatic when `confirm=True` | Never re-implement; never forward the flag (fact #3). |
| Job passthrough | `_build_tool_fn` `:940` returns the API body via `to_json` | 202 `{job_id, kind}` flows straight through (fact #4). Description **must** contain literal `wait_for_job(job_id)` (fact #4). |
| Schemas | `ModDownloadIn` `schemas/mod.py:111`, `ModVerifyIn` `:107`, `ModpackFromServerIn` `schemas/modpack.py:89` | Imports to add: fact #8. |
| Job-kind constants (for asserting `kind`) | `mod_download`, `verify_repair`, `mod_update_check`, `mod_update_apply` | fact #6. |
| Endpoint behaviour (404/409 shapes) | `backend/app/api/mods.py:218-327`, `modpacks.py:256-301` | fact #6 — read the exact route you mirror, nothing else. |
| Test file | `backend/tests/test_mcp_tools_mutations.py` | `MUTATION_TOOLS` set `:47`, `JOB_RETURNING_TOOLS` set `:88`, `JobReturningToolTests` `:432`, `ConfirmGateTests` `:190`. Helpers `_first_text` `:100`, `_result_json` `:104`, `_seed_server` `:157`, `mcp_session` `:164` (fact #9). |
| venv python (Windows) | `backend/.venv/Scripts/python.exe` | In an opencode brief write it `./.venv/Scripts/python.exe`. |

**Do not** read `.sprints/` — the brief + this file are the whole context. Do
not touch `test_mcp_end_to_end.py`, any schema, model, migration, route, or the
frontend.

---

## Phase 1 — S1 (native Claude, Sonnet 5)

### S1 — Four job-returning mod tools + shared plumbing · §1, §2, facts #4 #6 #8 #9

**Deliver.**

1. **Imports** in `backend/app/mcp/tools.py` (fact #8): extend the
   `from ..schemas.mod import ...` line (currently `ModAddIn`) to also import
   `ModDownloadIn, ModVerifyIn`.
2. **Four `ToolSpec` rows** appended to the `# Mods` group of `TOOL_SPECS`
   (after `delete_mod`, before the `# Modpacks` comment): `download_mod`,
   `verify_mods`, `check_all_mod_updates`, `apply_all_mod_updates`. Use the spec
   bodies in PLAN §1 verbatim as the starting point. Requirements:
   - `download_mod`: `method="POST"`, `path="/api/mods/{guid}/download"`,
     `arg_model=ModDownloadIn`, `confirm=True`.
   - `verify_mods`: `method="POST"`, `path="/api/mods/verify"`,
     `arg_model=ModVerifyIn`, `confirm=True`.
   - `check_all_mod_updates`: `method="POST"`,
     `path="/api/mods/updates/check"`, **no `arg_model`**, `confirm=True`.
   - `apply_all_mod_updates`: `method="POST"`,
     `path="/api/mods/updates/apply"`, **no `arg_model`**, `confirm=True`.
   - Every description `> 80` chars, states the side effect, and contains the
     literal `wait_for_job(job_id)`.
3. **Test set edits** in `backend/tests/test_mcp_tools_mutations.py`:
   - `MUTATION_TOOLS` (`:47`) — add all four names (put them near the `# mods`
     comment block).
   - `JOB_RETURNING_TOOLS` (`:88`) — add all four names.
4. **Four tests** in `JobReturningToolTests` (`:432`), each mirroring
   `test_scan_mods_returns_job_enqueued_out` (`:453`):
   - `test_download_mod_returns_job_enqueued_out` — the download route does
     `session.get(Mod, guid)` and 404s if absent, so **seed a `Mod` row first**:
     `from app.models import Mod` (add to the test's imports or inline), then in
     the test `async with self.sessions() as s: s.add(Mod(guid="A"*16)); await
     s.commit()`. Call
     `session.call_tool("download_mod", {"guid": "A"*16, "confirm": True})`.
     Assert `_result_json(result) == {"job_id": 7, "kind": "mod_download"}`.
   - `test_verify_mods_returns_job_enqueued_out` — call
     `{"confirm": True}` (no guids). Assert `{"job_id": 7, "kind":
     "verify_repair"}`.
   - `test_check_all_mod_updates_returns_job_enqueued_out` — call
     `{"confirm": True}`. Assert `{"job_id": 7, "kind": "mod_update_check"}`.
   - `test_apply_all_mod_updates_returns_job_enqueued_out` — call
     `{"confirm": True}`. Assert `{"job_id": 7, "kind": "mod_update_apply"}`.
     (`guard_update_scope(session, "all")` selects `Mod.is_local` → empty on the
     fresh DB → free-space guard passes against the fixture's patched
     `settings.mods_dir`. No seeding needed.)

**Done when.**
- `./.venv/Scripts/python.exe -m pytest -q` is green.
- `./.venv/Scripts/python.exe -m pytest -q tests/test_mcp_tools_mutations.py -k
  "download_mod or verify_mods or check_all_mod_updates or apply_all_mod_updates"`
  shows 4+ passing (the 4 new + the surface tests picking them up).
- `MutationToolSurfaceTests` still green (it now also checks the 4 new names for
  confirm-declaration + `wait_for_job` in the description).

**Watch out.**
- The `JOB_RETURNING_TOOLS` description assertion (`:519`) needs the **exact**
  substring `wait_for_job(job_id)` — not `wait_for_job` alone, not
  `wait_for_job(<job_id>)`.
- `download_mod` path param is `guid` → types as `str` (fact #7). Do **not** add
  it to `_PATH_PARAM_TYPES`.
- `verify_mods` / the two `updates/*` tools take no path param and no body arg —
  a bare `ToolSpec(name=..., method="POST", path=..., description=...,
  confirm=True)` is the whole row for the two `updates/*` ones.
- Keep the tuple's trailing comma and the `# Mods` / `# Modpacks` comments
  intact.

---

## Phase 2 — S2 (opencode · deepseek)

### S2 — `delete_mod_local` tool + test · §1, §2, fact #6 #9

Runs on the tree S1 left. Same two files.

**Deliver.**

1. One `ToolSpec` appended to the `# Mods` group of `TOOL_SPECS` in
   `backend/app/mcp/tools.py` (right after S1's `apply_all_mod_updates`, still
   before the `# Modpacks` comment):
   - `name="delete_mod_local"`, `method="DELETE"`,
     `path="/api/mods/{guid}/local"`, **no `arg_model`**, `confirm=True`.
   - Description from PLAN §1 (`delete_mod_local` block) — `> 80` chars, states
     the side effect, contrasts with `delete_mod`. It is **not** job-returning,
     so it must **not** contain `wait_for_job`.
2. `backend/tests/test_mcp_tools_mutations.py`: add `"delete_mod_local"` to the
   `MUTATION_TOOLS` set (`:47`, near the `# mods` names). Do **not** add it to
   `JOB_RETURNING_TOOLS`.
3. One test in `ConfirmGateTests` (`:190`),
   `test_delete_mod_local_gates_on_confirm_and_reaches_the_api`:
   - `from app.models import Mod` (inline in the test is fine).
   - Seed: `async with self.sessions() as s: s.add(Mod(guid="B"*16,
     is_local=False)); await s.commit()`.
   - `async with self.mcp_session() as session:`
     - no-confirm call `session.call_tool("delete_mod_local", {"guid":
       "B"*16})` → `refused.is_error` is True and `_first_text(refused)`
       contains `"confirm"`.
     - confirm call `session.call_tool("delete_mod_local", {"guid": "B"*16,
       "confirm": True})` → `done.is_error` is True and `_first_text(done)`
       contains `"409"` (the route answers *"mod has no on-disk files to
       remove"* — this proves the call reached the API without needing a real
       addon directory on disk).

**Done when.** `./.venv/Scripts/python.exe -m pytest -q` green; the new test
passes; `MutationToolSurfaceTests` green (it now also asserts `delete_mod_local`
declares `confirm: bool = False` and has a `> 80` char description).

**Watch out.**
- `DELETE` + a body-returning result → model the row on `unpin_server_mod`
  (`tools.py:654`: `method="DELETE"`, `confirm=True`, no `arg_model`), **not**
  on `delete_server` (which is a 204).
- Do not give it an `arg_model` — the route takes no body.
- Cover exactly the one test above. No success-path test, no disk fixture, no
  extra assertions.
- Leave S1's rows and every comment/among the tuple untouched.

The deliverable is written, passing code — not a plan. Read at most 3 files
(`backend/app/mcp/tools.py`, `backend/tests/test_mcp_tools_mutations.py`,
`backend/app/api/mods.py` lines 286-327), then your next tool call is an edit.

---

## Phase 3 — S3 (opencode · glm)

### S3 — `create_modpack_from_server` tool + test + README check + RESULTS.md · §1, §2, §3, fact #6 #9

Runs on the tree S2 left.

**Deliver.**

1. `backend/app/mcp/tools.py`:
   - Extend the `from ..schemas.modpack import ...` line (currently
     `ModpackApplyIn, ModpackCreate, ModpackUpdate`) to also import
     `ModpackFromServerIn`.
   - One `ToolSpec` appended to the `# Modpacks` group of `TOOL_SPECS`,
     **immediately after `apply_modpack`** (`tools.py:759`):
     - `name="create_modpack_from_server"`, `method="POST"`,
       `path="/api/modpacks/from-server/{server_id}"`,
       `arg_model=ModpackFromServerIn`, `confirm=True`.
     - Description from PLAN §1 (`create_modpack_from_server` block) — `> 80`
       chars, states the side effect, says pins are not carried. Not
       job-returning → no `wait_for_job`.
2. `backend/tests/test_mcp_tools_mutations.py`: add
   `"create_modpack_from_server"` to `MUTATION_TOOLS` (`:47`, near the
   `# modpacks` names). Not in `JOB_RETURNING_TOOLS`.
3. One test in `ConfirmGateTests` (`:190`),
   `test_create_modpack_from_server_gates_on_confirm_and_creates_via_the_api`,
   modelled on `test_clone_server_with_confirm_clones_via_the_api` (`:291`):
   - `from app.models import ServerMod` (inline is fine; `_seed_server` `:157`
     is already on the fixture).
   - `row = await self._seed_server("main")`; then
     `async with self.sessions() as s: s.add(ServerMod(server_id=row.id,
     mod_guid="C"*16, mod_name="CBA")); await s.commit()`.
   - `async with self.mcp_session() as session:`
     - no-confirm `session.call_tool("create_modpack_from_server", {"server_id":
       row.id, "name": "snap"})` → `refused.is_error` True, text contains
       `"confirm"`.
     - confirm `session.call_tool("create_modpack_from_server", {"server_id":
       row.id, "name": "snap", "confirm": True})` → `done.is_error` False;
       `_result_json(done)["name"] == "snap"`; `_result_json(done)["pins_note"]
       is None`.
     - `packs = _result_json(await session.call_tool("list_modpacks", {}))` →
       `[p["name"] for p in packs] == ["snap"]`.
4. **README check (§3):** open `README.md` around lines 106-116 ("Connect an
   agent"). It describes the surface generically and does **not** list
   individual tools → **make no change**. If (unexpectedly) a concrete tool
   enumeration is there, add the six new names to it and note that in RESULTS.md.
5. **Write `.sprints/5/RESULTS.md`** shaped like `.sprints/4/RESULTS.md`:
   status, a story-by-story table (S1/S2/S3, agent, what landed), "data-model
   changes: none", the new MCP tool surface (the six names + their endpoints +
   `confirm` + job-kind), verification performed (`pytest -q` output summary),
   an explicit **"not exercised"** list (Docker, live `/mcp`, job execution,
   `delete_mod_local` success path, `download_mod` free-space-trip path), minor
   observations, and a closing paragraph "how this sprint was implemented"
   naming which agent did which story and any stalls/restarts. Use absolute
   dates (sprint run: 2026-09-04).

**Done when.** `./.venv/Scripts/python.exe -m pytest -q` green; the new test
passes; `MutationToolSurfaceTests` green with `create_modpack_from_server`
included; `RESULTS.md` exists and matches the `.sprints/4` shape; README
unchanged (or updated + noted).

**Watch out.**
- `server_id` **is** in `_PATH_PARAM_TYPES` → types as `int` automatically. Good
  — do not touch that map.
- The route returns 201 with a body; the tool just passes it through. No special
  handling.
- `ModpackFromServerOut` extends `ModpackOut`, so `_result_json(done)` has
  `id`, `name`, `items`, `pins_note`. Assert only `name` and `pins_note`.
- Cover exactly the one test. Do not add a pins-present variant.
- Leave S1/S2 rows and all tuple comments intact.

The deliverable is written, passing code + RESULTS.md — not a plan. Read at most
4 files (`tools.py`, `test_mcp_tools_mutations.py`, `app/api/modpacks.py` lines
256-301, `.sprints/4/RESULTS.md` for shape), then your next tool call is an
edit.
