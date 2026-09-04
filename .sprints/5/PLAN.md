# Reforger Manager — Sprint 5 plan

## Context

Sprint 4 shipped the MCP administration interface as a table of `ToolSpec`
rows in `backend/app/mcp/tools.py` that mirror the REST API "1:1". A gap audit
(2026-09-04) against `backend/app/api/` found six mutating REST endpoints with
**no** MCP tool — all in the mod-lifecycle / modpack area:

| Missing tool | REST endpoint | Why it matters |
|---|---|---|
| `download_mod` | `POST /api/mods/{guid}/download` | An agent can `add_mod` (catalogue row only) but has **no way to pull the addon files to disk**. |
| `verify_mods` | `POST /api/mods/verify` | No way to verify/repair a corrupted addon cache. |
| `check_all_mod_updates` | `POST /api/mods/updates/check` | Only the server-scoped check is mirrored; no library-wide check. |
| `apply_all_mod_updates` | `POST /api/mods/updates/apply` | Same — no library-wide apply. |
| `delete_mod_local` | `DELETE /api/mods/{guid}/local` | Only full `delete_mod` (row + files) is mirrored; no "free the disk, keep the catalogue entry". |
| `create_modpack_from_server` | `POST /api/modpacks/from-server/{server_id}` | No way to snapshot a server's mod set into a reusable pack. |

## Guiding principle

These are **thin mirrors** of endpoints that already exist, tested, and guarded.
The tool layer holds no business logic (`tools.py:1-22`). Each new tool is one
`ToolSpec` tuple entry — same shape, same `confirm` gate, same job-passthrough
as the ~35 rows already there. No new schema, model, migration, route, or
frontend. If a story needs to change anything outside `tools.py` and the two
MCP test files, it is off-plan — stop and flag it.

## Decisions locked for this sprint

| # | Decision |
|---|---|
| 1 | **6 tools, 1:1 with the REST endpoints.** The "library wide update check apply" gap is two tools (`check_all_mod_updates` + `apply_all_mod_updates`), not one combined tool. |
| 2 | **Verification is pytest only.** `backend/.venv/Scripts/python.exe -m pytest -q` is the gate. No Docker, no live `/mcp` smoke. RESULTS.md lists Docker as *not exercised*. |
| 3 | **Only the six in the table.** Library-level pin/unpin and modpack export/import stay out — separate follow-up. |
| 4 | All six are `confirm=True`. Four are job-returning (`download_mod`, `verify_mods`, `check_all_mod_updates`, `apply_all_mod_updates`); two return a body (`delete_mod_local` → `ModOut`, `create_modpack_from_server` → `ModpackFromServerOut`). |
| 5 | Tests mirror `backend/tests/test_mcp_tools_mutations.py` only. No new test file; extend the existing one. `test_mcp_end_to_end.py` is **not** touched. |

## Code-level facts the implementation must respect

1. **The tool table is `TOOL_SPECS` in `backend/app/mcp/tools.py:186-917`** — a
   `tuple[ToolSpec, ...]`. `register_tools` (`:1042`) just iterates it. Adding a
   tool = adding one `ToolSpec(...)` entry to that tuple. Nothing else is wired.
2. **`ToolSpec` fields** (`:55-78`): `name, method, path, description,
   arg_model=None, query_model=None, multipart_field=None, confirm=False,
   timeout_s=60.0`. `_build_tool_fn` (`:940`) turns `{name}` path placeholders
   into required args, `arg_model` fields into a JSON body on non-GET / query
   params on GET, and appends `confirm: bool = False` when `confirm=True`.
3. **The confirm gate** (`:977-983`): a `confirm=True` spec raises
   `ToolError("API 400: this tool changes real state — re-run it with
   confirm=true to proceed (no API call was made)")` unless `confirm=True` is
   passed. The flag is never forwarded to the API. Do not re-implement — just
   set `confirm=True` on the spec.
4. **Job-returning tools** return the API's `JobEnqueuedOut` (`{job_id, kind}`)
   verbatim (202 passthrough — `_build_tool_fn` does nothing special, the body
   just flows through `to_json`). Their `description` **must contain the literal
   substring `wait_for_job(job_id)`** — `test_job_tool_descriptions_document_wait_for_job`
   (`test_mcp_tools_mutations.py:519`) asserts it for every name in
   `JOB_RETURNING_TOOLS`.
5. **Every mutation description must be > 80 chars** and state the side effect —
   `test_every_mutation_description_states_its_side_effect` (`:527`) asserts it
   for every name in `MUTATION_TOOLS`. Match the register of the existing rows
   (see `scan_mods` `:667`, `add_mod` `:680`, `delete_mod` `:694`,
   `apply_modpack` `:744`).
6. **Endpoint facts** (from `backend/app/api/mods.py` + `modpacks.py`):
   - `POST /api/mods/{guid}/download` (`mods.py:267`) — body `ModDownloadIn`
     (`schemas/mod.py:111`, one optional `version: str | None`), route accepts
     `body: ModDownloadIn | None = None`. 404 if the guid is not a library row;
     runs the free-space guard against `settings.mods_dir`; enqueues
     `MOD_DOWNLOAD_JOB_KIND = "mod_download"` (`app/mods/downloader.py:30`).
   - `POST /api/mods/verify` (`mods.py:231`) — body `ModVerifyIn | None = None`
     (`schemas/mod.py:107`, one optional `guids: list[str] | None`). Enqueues
     `VERIFY_REPAIR_JOB_KIND = "verify_repair"` (`app/mods/verify.py:18`).
   - `POST /api/mods/updates/check` (`mods.py:218`) — **no body**. Enqueues
     `MOD_UPDATE_CHECK_JOB_KIND = "mod_update_check"` with `params={"scope":
     "all"}` (`app/mods/updates.py:27`).
   - `POST /api/mods/updates/apply` (`mods.py:224`) — **no body**. Calls
     `guard_update_scope(session, "all")` (`app/mods/freespace.py:56` — selects
     `Mod.is_local`, projects, `check_free_space(settings.mods_dir, ...)`), then
     enqueues `MOD_UPDATE_APPLY_JOB_KIND = "mod_update_apply"`
     (`app/mods/updates.py:28`). 409 while a server runs / guard trips.
   - `DELETE /api/mods/{guid}/local` (`mods.py:286`) — **no body**, returns
     `ModOut` (200). 409 while any server runs, 404 unknown guid, 409 if
     `not mod.is_local` ("mod has no on-disk files to remove"), 409 if still
     directly referenced or held by a dependency closure, 400 if the addon dir
     resolves outside the cache root. Keeps the `mods` row, clears `is_local`.
   - `POST /api/modpacks/from-server/{server_id}` (`modpacks.py:256`) — body
     `ModpackFromServerIn` (`schemas/modpack.py:89`, `name: str`,
     `description: str | None`). Returns `ModpackFromServerOut`
     (`schemas/modpack.py:94` — `ModpackOut` + `pins_note: str | None`), 201.
     Snapshots `server.mods` order into a new pack; pins are **not** carried,
     `pins_note` names them when present. 404 unknown server, 409 name
     collision. Never mutates the source server.
7. **`_PATH_PARAM_TYPES` (`tools.py:172`)** maps `server_id`/`pack_id`/`job_id`
   to `int`; `guid` is absent so it types as `str` — correct, matches the
   existing `get_mod` / `delete_mod` / `pin_server_mod` rows that use `{guid}`.
8. **Imports to add at the top of `tools.py`**: `ModDownloadIn, ModVerifyIn`
   from `..schemas.mod` (line 40 currently imports only `ModAddIn`);
   `ModpackFromServerIn` from `..schemas.modpack` (line 41).
9. **Test harness** — `backend/tests/test_mcp_tools_mutations.py`:
   - `MUTATION_TOOLS` set (`:47`) and `JOB_RETURNING_TOOLS` set (`:88`) — the
     six new names go here (four also into `JOB_RETURNING_TOOLS`). The surface
     tests (`MutationToolSurfaceTests`, `:498`) then cover them for free.
   - `JobReturningToolTests` (`:432`) already patches `job_manager.enqueue`
     → `7` and `settings.mods_dir` → a tmpdir. Add one test per job tool
     mirroring `test_scan_mods_returns_job_enqueued_out` (`:453`).
     **`download_mod` additionally needs a seeded `Mod` row** (route does
     `session.get(Mod, guid)` → 404) — `from app.models import Mod`, add it in
     the test body.
   - `ConfirmGateTests` (`:190`) — add a confirm-gate + reaches-API test for
     `delete_mod_local` (seed a `Mod(guid=..., is_local=False)`; with
     `confirm=True` the route answers 409 "no on-disk files", proving the call
     landed — no disk fixture needed) and for `create_modpack_from_server`
     (mirror `test_clone_server_with_confirm_clones_via_the_api` `:291` — seed a
     server + a `ServerMod`, assert the pack appears via `list_modpacks` and
     `pins_note` is null).

## Feature set

### 1. Six `ToolSpec` rows in `TOOL_SPECS`

Placement: the five mod rows go in the existing `# Mods` group (after
`delete_mod`, `tools.py:705`, before `# Modpacks`). `create_modpack_from_server`
goes in the `# Modpacks` group after `apply_modpack` (`:759`).

```python
# --- in the # Mods group ---
ToolSpec(
    name="download_mod",
    method="POST",
    path="/api/mods/{guid}/download",
    description=(
        "Force a (re)download of one library mod's addon files by 16-hex GUID, "
        "optionally pinned to an exact Workshop version. Side effect: writes to "
        "the mod disk cache (steamcmd). 404 if the GUID is not a library row, "
        "400/409 if the free-space guard trips. Returns the enqueued job "
        "({job_id, kind}) immediately — poll it with wait_for_job(job_id)."
    ),
    arg_model=ModDownloadIn,
    confirm=True,
),
ToolSpec(
    name="verify_mods",
    method="POST",
    path="/api/mods/verify",
    description=(
        "Enqueue the verify_repair job: re-check downloaded addon files against "
        "their Workshop manifests and re-fetch anything missing or corrupt. "
        "guids omitted = the whole local library; pass a guids list to scope "
        "it. Side effect: may rewrite the mod disk cache. Returns the enqueued "
        "job ({job_id, kind}) immediately — poll it with wait_for_job(job_id)."
    ),
    arg_model=ModVerifyIn,
    confirm=True,
),
ToolSpec(
    name="check_all_mod_updates",
    method="POST",
    path="/api/mods/updates/check",
    description=(
        "Enqueue a library-WIDE mod-update-check job: refresh latest Workshop "
        "versions and pin staleness for every mod in the library — downloads "
        "nothing. Use check_server_mod_updates instead to scope it to one "
        "server's assigned set. Returns the enqueued job ({job_id, kind}) "
        "immediately — poll it with wait_for_job(job_id), then list_mods "
        "(update=true) to see what is newer."
    ),
    confirm=True,
),
ToolSpec(
    name="apply_all_mod_updates",
    method="POST",
    path="/api/mods/updates/apply",
    description=(
        "Enqueue a library-WIDE mod-update-apply job: download the newer "
        "Workshop version of every library mod that has one and reconcile pins "
        "against the current engine build. Side effect: writes to the mod disk "
        "cache. 409 while any server is running or the free-space guard trips. "
        "Returns the enqueued job ({job_id, kind}) immediately — poll it with "
        "wait_for_job(job_id)."
    ),
    confirm=True,
),
ToolSpec(
    name="delete_mod_local",
    method="DELETE",
    path="/api/mods/{guid}/local",
    description=(
        "Delete one mod's on-disk addon files but KEEP its catalogue row "
        "(is_local flips to false) — the disk-space counterpart of delete_mod, "
        "which removes the row too. Side effect: removes the addon directory. "
        "409 while any server runs, if the mod has no on-disk files, or while "
        "it is still referenced by a server/modpack or another mod's resolved "
        "dependency closure; 404 for an unknown GUID. Returns the updated mod."
    ),
    confirm=True,
),
# --- in the # Modpacks group, after apply_modpack ---
ToolSpec(
    name="create_modpack_from_server",
    method="POST",
    path="/api/modpacks/from-server/{server_id}",
    description=(
        "Snapshot a server definition's current mod set and load order into a "
        "NEW modpack (name required, optional description). Side effect: a new "
        "modpack row. Pins are NOT carried into the pack (a pack is a mod list, "
        "not a version lock) — pins_note in the response names any that were "
        "dropped. The source server is never modified. 404 for an unknown "
        "server; 409 on a modpack name collision."
    ),
    arg_model=ModpackFromServerIn,
    confirm=True,
),
```

Wording above is a starting point — the implementer may tighten it, but must
keep: `> 80` chars, the side-effect sentence, and (job tools only) the literal
`wait_for_job(job_id)`.

### 2. Test coverage — extend `backend/tests/test_mcp_tools_mutations.py`

- Add the six names to `MUTATION_TOOLS`; add `download_mod`, `verify_mods`,
  `check_all_mod_updates`, `apply_all_mod_updates` to `JOB_RETURNING_TOOLS`.
- `JobReturningToolTests`: `test_download_mod_returns_job_enqueued_out`
  (seed a `Mod` row), `test_verify_mods_returns_job_enqueued_out`,
  `test_check_all_mod_updates_returns_job_enqueued_out`,
  `test_apply_all_mod_updates_returns_job_enqueued_out`. Each: call with
  `{"confirm": True}` (plus `guid`/`guids` where relevant), assert
  `_result_json(result) == {"job_id": 7, "kind": "<kind>"}`.
- `ConfirmGateTests`:
  `test_delete_mod_local_gates_on_confirm_and_reaches_the_api` — seed
  `Mod(guid=..., is_local=False)`; no-confirm call → `is_error` + "confirm";
  `confirm=True` call → `is_error` + "409" (proves the route ran).
  `test_create_modpack_from_server_gates_on_confirm_and_creates_via_the_api` —
  seed a server + one `ServerMod`; no-confirm → refused; `confirm=True` →
  `_result_json(done)["name"] == "<pack>"` and `pins_note is None`, and
  `list_modpacks` now lists it.
- **Cover exactly these — no more.** No success-path disk fixture for
  `delete_mod_local`, no job-execution assertions (the sqlite harness registers
  no handlers), no `test_mcp_end_to_end.py` changes.

### 3. README

`README.md:106-116` ("Connect an agent") describes the surface generically
("the REST verbs mirrored 1:1 plus three read-only composites") and does **not**
enumerate tools. **No change required.** Confirm this during S3 and note it in
RESULTS.md; touch the file only if a concrete tool list is found.

## Data-model changes

None.

## Testing

Enumerated in Feature set §2. Proportionate: ~6 new test methods + two set
edits. The existing `MutationToolSurfaceTests` gives registration / confirm-
declaration / description-length / `wait_for_job` coverage automatically once
the names are in the two sets.

## Cut from this sprint / Non-goals

- Library-level `pin`/`unpin` tools (`POST`/`DELETE /api/mods/{guid}/pin`).
- Modpack `export`/`import` tools.
- Binary `GET .../files/download` and `.../files/archive` (not useful over MCP).
- Any frontend work — these endpoints already have their webui surface.
- Docker / live `/mcp` verification.
- Committing or pushing.

## Verification

```bash
cd backend && ./.venv/Scripts/python.exe -m pytest -q
```

Must be green, with the new tests visibly collected. Spot-check:

```bash
cd backend && ./.venv/Scripts/python.exe -m pytest -q tests/test_mcp_tools_mutations.py
```

Manual live check: **none** (decision #2).
