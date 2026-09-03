# Reforger Manager — Sprint 3 implementation stories

> Context, decisions and the full feature spec live in [PLAN.md](PLAN.md).
> `§n` = PLAN.md *Feature set* number; `fact #n` = its *Code-level facts* list.
> Every line/path reference below was verified against the tree at sprint start —
> if a line number has drifted, trust the symbol name.

## Shape of the sprint

Nine stories, four phases. The backend is built bottom-up (safety primitive → read routes →
write routes) so no route can be written before the resolver that makes it safe exists. The
frontend follows once the API is real.

| Phase | Stories | Theme |
|---|---|---|
| 0 | S1 | Settings + path-safety primitive |
| 1 | S2–S3 | Backend routes (read side, then write side) |
| 2 | S4–S7 | Files tab in the SPA |
| 3 | S8–S9 | Docs + live verification |

**Parallelism.** S1 blocks everything. S2 and S3 both depend on S1, and S3 edits the router
module S2 creates — do S2 first. S4 depends on S2+S3 being merged; S5–S7 depend on S4 and run in
sequence in one file. S8 is independent of the frontend and can run alongside Phase 2.

Each story lists **Deliver**, **Done when**, **Watch out**. Backend stories carry pytest coverage
in the style of `backend/tests/`; the frontend stays untested (`npm run build` is the only gate)
per Sprint-2 precedent.

**Verification for every story**: `cd backend && python -m pytest` and, for frontend stories,
`cd frontend && npm run build`.

---

## Shared reference (read before starting any story)

### Backend anchors

| Thing | Where | Notes |
|---|---|---|
| Profile dir path | `app/core/config.py:105` `settings.profile_dir(id)` → `profiles_dir / str(id)` | Computed only. Created lazily by `supervisor.start` (`app/servers/supervisor.py:167`). **May not exist** (fact #1). |
| Path-safety precedent | `app/mods/logview.py:88` `_is_relative_to`, `:96` `current_log` | Triple `.resolve(strict=False)` ancestry check `profiles_root ⊃ profile_dir ⊃ logs_dir ⊃ candidate`. Generalize it, don't re-derive (fact #2). |
| Running-server check | `app/api/servers.py:149`, `:397`, `:483` | Verbatim: `supervisor.active_server_id != server_id or not supervisor.is_running()` (fact #3). |
| Attachment response | `app/api/servers.py:451-456` (`get_log`, `download=true`) | `Response(bytes, media_type=..., headers={"Content-Disposition": ...})` (fact #8). |
| Generated config route | `app/api/servers.py:284` `get_generated_config` | `ServerConfigOut{server_id, config, path}`; `?write=true` is the only disk touch. Reused as-is (fact #5). |
| Router registration | `app/main.py:283-293`, `_API = "/api"` | Add the new router to this block. |
| Auth dep | every `app/api/*.py`: `authed = [Depends(get_current_user)]` | Same pattern in the new router. |
| Definition lookup | `app/api/servers.py:78` `_load(session, server_id)` | 404s on an unknown id. Every new route needs the same 404-before-filesystem-work behaviour. |
| Multipart | `python-multipart==0.0.20`, `backend/requirements.txt:20` | Already present — no dependency change. This is the app's **first** `UploadFile` route. |

### Test conventions

- Pure-module tests: `tests/test_logview.py` — `monkeypatch.setattr(settings, "profiles_dir", tmp_path)`.
- Route tests: `tests/test_scenarios_routes.py` / `tests/test_storage_and_download.py` —
  `unittest.IsolatedAsyncioTestCase`, a throwaway `FastAPI()` carrying only the router under test,
  `app.dependency_overrides[get_session]` + `[get_current_user] = lambda: None`, driven by
  `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`. sqlite in-memory, no Postgres.
- Windows dev box: `os.symlink` needs Developer Mode / admin. Guard symlink tests so they skip on
  `OSError` — CI (Linux) still covers them.

### Frontend anchors

| Thing | Where | Notes |
|---|---|---|
| Tab tuple + panel switch | `src/pages/ServerDetail.tsx:13` and `:137-142` | `const tabs = ["Config","Mods","Console","Players","History"] as const;` |
| JSON textarea + parse gate | `src/components/server/ServerForm.tsx:841-860` (`gpParse` / `extraParse`) | Copy the shape: parse on every keystroke, render `<span className="error">`, disable Save (fact #6). |
| Running banner | `src/components/server/ServerForm.tsx:578` + `:614-618` | `border border-amber-700 bg-amber-950/50 px-3 py-2 text-xs text-amber-300`. |
| JSON transport | `src/lib/api.ts:137` `api<T>()`, `:151` `apiVoid()` | JSON in/out only — unusable for multipart and blobs (fact #7). |
| Token for raw `fetch` | `src/lib/api.ts:135` `getAccessToken()` | Raw fetches must set `Authorization: Bearer …` themselves and dispatch `new Event("auth:expired")` on 401, as `api()` does. |
| Blob download precedent | `src/components/settings/BackupCard.tsx:65-77` | `URL.createObjectURL` → hidden `<a download>` → `click()` → `revokeObjectURL`. |
| UI kit | `src/components/ui.tsx` | `Button` (variants `ghost`/`outline`, size `sm`), `Card`, `CardContent`, `Input`, `Badge`, `Dialog{open,title,children,onClose}` (`:84`). |
| Error text helper | `errText(error, fallback)` — local copies in `ModsPanel.tsx:70`, `ServerForm.tsx:76`, `pages/Modpacks.tsx:34` | Follow the local-copy convention; don't invent a shared module. |

---

## Phase 0 — Settings + path-safety primitive

### S1 — `servers/files.py`: safe path resolution and directory listing · §1, §3, facts #1 #2

**Deliver.**

1. `backend/app/core/config.py`, in the `# --- behaviour ---` block:
   - `files_max_edit_bytes: int = 2_097_152`
   - `files_max_upload_bytes: int = 50_000_000`

   Mirror both into `.env.example` under *Behaviour (optional; sensible defaults)* with a
   one-line comment each.

2. New `backend/app/servers/files.py`:
   - `class UnsafePath(Exception)` — the API layer maps it to 400.
   - `EXCLUDED_TOP_LEVEL = frozenset({"logs", "addons_tmp"})` — filtered out of listings at
     **this** layer, and rejected as a path prefix by the resolver so no route can reach into
     them by spelling the path out (PLAN decision table).
   - `resolve_safe_path(server_id: int, rel_path: str | None) -> Path`:
     reject *before* resolution — `Path(rel).is_absolute()`, a Windows drive/UNC prefix, any `..`
     segment, any `\x00`; empty-after-strip means the root and is allowed. Then resolve
     `strict=False` and re-check `profiles_root ⊃ profile_dir ⊃ candidate` using
     `logview._is_relative_to` (move it here and have `logview` import it, or leave `logview`'s
     copy alone — but do not fork the logic). Also reject a first segment in `EXCLUDED_TOP_LEVEL`.
   - Symlink rule (unconditional): walk each component of the path and reject if
     `Path.is_symlink()` on any of them — inside-the-profile targets included.
   - `@dataclass(frozen=True) FileEntry { name, rel_path, type: Literal["file","dir","symlink"], size: int, mtime: datetime | None, is_text: bool, is_json: bool }`.
     `is_text`: best-effort — read up to ~8 KiB, false on a NUL byte or `UnicodeDecodeError`;
     false unconditionally for `dir`/`symlink` and for files over `settings.files_max_edit_bytes`.
     `is_json`: `is_text` **and** `.json` suffix **and** `json.loads` of the full file succeeds.
   - `list_dir(server_id, rel_path) -> list[FileEntry]` — non-recursive; returns `[]` (not an
     error) when the profile dir or sub-path does not exist (fact #1); dirs first, then files,
     each sorted case-insensitively by name. Symlinks are **listed** (type `"symlink"`, `size=0`,
     `is_text=False`) — only traversing/reading them is refused.
   - `read_text_file` / `write_text_file` / `delete_path` / `make_dir` / `rename_path` /
     `build_archive_bytes` as thin resolver-first wrappers, so the API layer never constructs a
     `Path` itself.
   - `build_archive_bytes(server_id) -> bytes`: `zipfile.ZipFile` over `io.BytesIO` with
     `ZIP_DEFLATED`, `os.walk(..., followlinks=False)`, skipping every symlink and the excluded
     top-level dirs; arcnames relative to the profile dir. Returns a valid empty zip when the
     profile dir is missing.

3. `backend/tests/test_server_files.py` (module-level, no HTTP) — every PLAN *Path-safety* case:
   `../../etc/passwd`, `/etc/passwd`, `C:\Windows\win.ini`, bare `..`, embedded `\x00`, a symlink
   pointing outside the profile dir, a symlink pointing **inside** it (still refused),
   `logs/console.log`, and the same relative path resolved for server 7 vs server 8 landing in
   different dirs and never crossing. Plus: missing profile dir → `list_dir` returns `[]`;
   `is_json` false for a `.json` file with a syntax error; `is_text` false for bytes with a NUL.

**Done when.** `pytest tests/test_server_files.py` is green and nothing else regresses (the
`logview` tests especially, if `_is_relative_to` moved).

**Watch out.**
- `Path("foo/../bar")` is not reliably caught by `.resolve()` on a non-existent path — reject
  `..` **textually, before** resolving. That is the "defense in depth" the PLAN asks for.
- `Path(base) / "/etc/passwd"` **discards the base** in `pathlib`. The absolute-path check is
  load-bearing, not cosmetic.
- `settings.profiles_dir` is a `Path` from env; resolve it once *per call*. A module-level
  constant computed at import time makes monkeypatched tests leak into each other.
- `is_json` reads the whole file — gate it behind the size cap, or building a listing will read a
  500 MB `.json`.

---

## Phase 1 — Backend routes

### S2 — Read-side routes: list, content, download, archive · §2, facts #1 #5 #8

**Deliver.**

1. `backend/app/schemas/server_files.py` (shape it like `app/schemas/storage.py`):
   `FileEntryOut`, `DirListingOut{server_id, path, entries}`,
   `FileContentOut{path, size, editable: bool, is_json: bool, content: str | None}`, plus the S3
   request bodies (`WriteContentIn{content}`, `MkdirIn{path}`, `RenameIn{path, new_path}`).

2. New `backend/app/api/server_files.py`, with a module docstring listing every route in
   `api/servers.py`'s header style:

   ```python
   router = APIRouter(prefix="/servers/{server_id}/files", tags=["server-files"])
   authed = [Depends(get_current_user)]
   ```

   Registered in `app/main.py` alongside the other `include_router(..., prefix=_API)` calls
   (`main.py:283-293`), **after** `servers_api.router`.

   Every route first resolves the definition (import `_load` from `api.servers` or re-implement
   the three-line `select(Server)` lookup) so an unknown id is a 404 before any filesystem work.
   `UnsafePath` → `HTTPException(400)`.

   - `GET /` — `?path=` (default root) → `DirListingOut`. Empty list, **not 404**, when the
     profile dir does not exist (fact #1). 400 when the path resolves to a file.
   - `GET /content` — `?path=`. Under `settings.files_max_edit_bytes` and `is_text` →
     `{path, size, editable: true, is_json, content}`. Otherwise
     `{path, size, editable: false, is_json: false, content: null}` — a **200 marker, not an
     error** — so the UI can offer download instead. 404 for a missing file.
   - `GET /download` — `?path=`, any size/type:
     `Response(payload, media_type="application/octet-stream", headers={"Content-Disposition": ...})`,
     copying `get_log`'s download branch (`servers.py:451-456`). Sanitize the filename in the
     header (strip quotes/newlines).
   - `GET /archive` — whole profile dir as `.zip`, `media_type="application/zip"`, attachment
     name `server-{id}-profile.zip`. Symlinks skipped, never followed.

3. `backend/tests/test_server_files_routes.py` (route-test pattern above): listing a seeded tree;
   `logs/` and `addons_tmp/` absent from the listing; missing profile dir → `200` with an empty
   `entries`; an oversized file → `editable: false` from `/content` **and** still reachable via
   `/download`; a binary file → same; `/archive` unzipped in memory with
   `zipfile.ZipFile(io.BytesIO(response.content))`, asserting the symlink was skipped and every
   other file round-trips byte-for-byte; unknown `server_id` → 404; `?path=../..` → 400.

**Done when.** All four routes are green under pytest, and `GET /api/servers/{id}/files` on a
never-started definition returns an empty listing.

**Watch out.**
- Use `Response(bytes, ...)`, not `FileResponse` — the latter hands a raw host path to Starlette
  and re-stats it outside the resolver's guarantees.
- Read `settings.files_max_edit_bytes` at call time, never as a default-argument value — tests
  monkeypatch it down to a handful of bytes.
- Build the archive fully in memory (profile dirs are small). Do not stream from a
  `NamedTemporaryFile`; Windows dev boxes cannot reopen one while it is open.

---

### S3 — Write-side routes + running-server 409s · §2, fact #3

**Deliver.** In the same `app/api/server_files.py`:

- One guard used by all five mutating routes (fact #3 — the same condition as `delete_server` /
  `stop_server` / modpack-apply):

  ```python
  def _refuse_while_running(server_id: int) -> None:
      if supervisor.active_server_id == server_id and supervisor.is_running():
          raise HTTPException(status.HTTP_409_CONFLICT, "stop the server before editing its profile files")
  ```

- `PUT /content` — `?path=`, body `WriteContentIn{content}`. Overwrites an existing file, creates
  a missing one; a **missing parent directory is a 400** telling the caller to `mkdir` first
  (keeps `PUT` from silently building a tree). For a `.json` suffix, `json.loads(content)`
  server-side and 400 on failure — the client gate is never trusted (PLAN *Testing*). Write to a
  temp file in the same directory then `os.replace`, so a failed write cannot truncate the
  original.
- `DELETE /` — `?path=` file or directory; directories go recursively via `shutil.rmtree`, with
  the resolver re-checked immediately before the call. Deleting the profile root itself is a 400.
- `POST /mkdir` — body `MkdirIn{path}`, `mkdir(parents=True, exist_ok=True)`.
- `POST /rename` — body `RenameIn{path, new_path}`, both through `resolve_safe_path`. 400 if the
  destination exists, if either resolves to the root, or if `new_path` is inside `path`
  (a directory moved into itself).
- `POST /upload` — `?path=` target directory, `files: list[UploadFile] = File(...)`. Per file:
  defeat a smuggled `..` by feeding `f"{target_rel}/{Path(filename).name}"` back through
  `resolve_safe_path`; enforce `settings.files_max_upload_bytes` → **413**, reading in chunks and
  aborting at the cap rather than buffering the whole body first; 400 when the target directory
  does not exist. Returns the written entries as `FileEntryOut`s.

Tests appended to `tests/test_server_files_routes.py`: the full 409 matrix — all five mutating
routes refused with `supervisor` monkeypatched to "this server is running", while `GET /`,
`/content`, `/download` and `/archive` all still return 200 in that same state; invalid JSON on a
`.json` `PUT` → 400 with the on-disk file unchanged; happy-path multipart upload; oversized
upload → 413; `filename="../../evil.txt"` → 400 and nothing written outside the profile dir;
recursive directory delete; rename onto an existing name → 400.

**Done when.** `pytest` is green and the 409 matrix (5 refused / 4 allowed) is asserted
explicitly.

**Watch out.**
- `supervisor` is a module-level singleton (`app/servers/supervisor.py`) — monkeypatch its
  attributes; never instantiate a second one.
- `active_server_id` is a **property** (`supervisor.py:81`) — patch the backing state, or use
  `patch.object(type(supervisor), "active_server_id", PropertyMock(...))`.
- `UploadFile.filename` is attacker-controlled and may be `None` or `""` — reject both.
- `os.replace` is only atomic within one directory; keep the temp file next to the target.

---

## Phase 2 — Files tab in the SPA

### S4 — Types + transport helpers for the files API · §4, fact #7

**Deliver.** In `frontend/src/lib/api.ts`:

- Types mirroring the S2 schemas: `FileEntry`, `DirListing`, `FileContent`.
- Because `api<T>()` is JSON-only (fact #7), three raw-`fetch` helpers next to `apiVoid` — each
  setting `Authorization: Bearer ${getAccessToken()}` and dispatching `auth:expired` on 401,
  exactly as `api()` does:
  - `apiUpload<T>(path, files: File[]): Promise<T>` — builds `FormData`, appends each file under
    the backend's field name (`files`), and **must not** set `Content-Type`.
  - `apiBlob(path): Promise<Blob>` — for `/download` and `/archive`.
  - `saveBlob(blob, filename)` — the `createObjectURL` → hidden `<a download>` →
    `revokeObjectURL` dance, lifted from `BackupCard.tsx:65-77`.

**Done when.** `npm run build` passes and no existing module changed behaviour.

**Watch out.** Setting `Content-Type: multipart/form-data` by hand silently breaks the upload —
the boundary goes missing and FastAPI sees an empty form. Leave the header off entirely.

---

### S5 — Files tab shell + directory browser · §4

**Deliver.**

- `frontend/src/pages/ServerDetail.tsx`: add `"Files"` to the `tabs` tuple (`:13`), after
  `"Mods"`, and render `{tab === "Files" && <FilesPanel id={id} server={server} />}` in the
  switch (`:137-142`).
- New `frontend/src/components/server/FilesPanel.tsx`:
  - `useState` for the current relative path (`""` = root); `useQuery` keyed
    `["server-files", id, path]` → `GET /api/servers/${id}/files?path=${encodeURIComponent(path)}`.
  - Breadcrumb built from the path segments (root crumb labelled `profile/`), each crumb
    clickable.
  - Table rows: name (type icon; directories navigate on click), human-readable size, locale
    modified time, and a per-row action cluster (stubbed here, wired in S6/S7).
  - The running banner from `ServerForm.tsx:614-618` when `server.is_running`, reading *"This
    server is running — stop it to add, edit, rename or delete profile files."* Every mutating
    control is `disabled` while running, so the backend 409 is a backstop, not the UX.
  - Empty state via `components/Empty.tsx`; for a never-started definition say so explicitly
    ("no profile directory yet — start the server once").
  - A collapsed `<details>` **"Generated config.json (read-only)"** at the top of the tab, from
    the existing `GET /api/servers/${id}/config` (fact #5; `ConfigResponse` is already typed at
    `lib/api.ts:102`), rendered as a `<pre>` of `JSON.stringify(config, null, 2)`. No write path.

**Done when.** `npm run build` passes, the tab lists a real profile directory and navigates into
subfolders, and `logs/` never appears.

**Watch out.** `encodeURIComponent` every path that goes into a query string — persistence
filenames contain spaces and `#`. Invalidate the `["server-files", id]` *prefix* (not the exact
key) after a mutation, so the current directory refetches.

---

### S6 — File editor dialog with the JSON gate · §4, fact #6

**Deliver.** In `FilesPanel.tsx`:

- **Open** on rows where `is_text` is true → `GET /content`. If the response comes back
  `editable: false`, do not open the dialog — show an inline note and offer Download instead
  (this is the size/binary fallback, and it arrives as a 200, not an error).
- A `Dialog` (`ui.tsx:84`) holding a monospace `<textarea>` (same classes as
  `ServerForm.tsx:845`) with Save / Cancel. When `is_json`, parse on every change exactly like
  `gpParse`/`extraParse` (`ServerForm.tsx:841-860`), render the parse error under the textarea,
  and keep Save disabled while it fails.
- Save → `PUT /content?path=…` with `{content}` via `api()`; on success close, invalidate
  `["server-files", id]`, and surface a 409 (server started mid-edit) as the banner text rather
  than a raw error string.
- **Download** on every file row via `apiBlob` + `saveBlob`. Symlink rows get no actions at all —
  they are visible but inert.
- **Download all as .zip** in the panel header → `GET /archive`.

**Done when.** `npm run build` passes, a `.json` file with a typo cannot be saved, and a 3 MB file
offers only Download.

**Watch out.** Seed the dialog's local text state from the fetched content **once** — a
`useEffect` that re-seeds on every refetch will discard the operator's in-progress edit.

---

### S7 — Mutating controls: new folder, upload, rename, delete · §4

**Deliver.** In `FilesPanel.tsx`, all disabled while `server.is_running`:

- **New Folder** — a `Dialog` with an `Input`, then `POST /mkdir` with the name joined onto the
  current path.
- **Upload** — a hidden `<input type="file" multiple ref={…}>` plus a Button that clicks it (the
  `fileInputRef` pattern in `BackupCard.tsx`), then `apiUpload` to `POST /upload?path=${current}`.
  Surface per-file errors (413 in particular) rather than one opaque failure, and reset the
  input's `value` afterwards so re-picking the same file still fires `onChange`.
- **Rename** — a `Dialog` pre-filled with the current name → `POST /rename`.
- **Delete** — confirm in place (a second click flips the button to "Confirm", as
  `ServerForm.tsx`'s `confirmDelete` does); for a directory the confirm copy names it and says the
  contents go with it. `DELETE /?path=`.
- Every mutation invalidates `["server-files", id]`.

**Done when.** `npm run build` passes, each control round-trips against a live backend, and every
one of them is visibly disabled with the banner shown while this definition is the running one.

**Watch out.** A rename that only changes case is a no-op on a case-insensitive filesystem and a
real move on the NAS — don't special-case it; let the backend answer.

---

## Phase 3 — Docs and live verification

### S8 — Documentation · §3

**Deliver.**

- `.env.example`: `FILES_MAX_EDIT_BYTES` / `FILES_MAX_UPLOAD_BYTES` with a one-line comment each
  (verify and finish if S1 already added them).
- `README.md`: a short *Profile files* subsection — what the tab reaches
  (`PROFILES_DIR/{id}/`), what it deliberately does not (`SERVER_DIR`, `MODS_DIR`, `CONFIGS_DIR`,
  `logs/`, `addons_tmp/`), the stop-to-edit rule, and the no-backup caveat from PLAN *Cut from
  this sprint*.
- `CLAUDE.md` **Gotchas**: one bullet — profile files are editable through the UI, but
  `config.json` is regenerated from the DB on every start, so hand-edits under `CONFIGS_DIR` are
  clobbered.

**Done when.** All three describe the shipped behaviour, and the `FILES_MAX_*` names in
`.env.example` match `core/config.py` exactly.

**Watch out.** Do not document a `.bak`/versioning safety net — it was explicitly declined.

---

### S9 — Live verification against the JLH stack · PLAN *Verification*

**Deliver.** Against a running stack (`docker compose up -d --build`):

1. Create the JLH Conflict PvE Framework definition through the existing `POST /api/servers`
   (mods + scenario ID from the serverpack doc).
2. Start it once so `supervisor.start` creates `PROFILES_DIR/{id}/`, then stop it.
3. Through the Files tab only — never SSH, never touch the NAS filesystem by hand — browse what
   JLH/MrSaint actually wrote, open and edit the relevant JSON, upload/rename/delete as needed,
   pull the `.zip`, and restart.
4. Confirm QRF/RECAP factions per the serverpack doc's §9 functional-test checklist.

Record what was found under the profile dir (filenames, layout, flat vs nested) in
`.sprints/3/RESULTS.md` — that is the first real-world data point on how Reforger mods persist,
and PLAN *Context* is explicit that no convention is documented anywhere.

**Done when.** The checklist passes and `RESULTS.md` exists, shaped like `.sprints/2/RESULTS.md`.

**Watch out.** This story mutates a real deployment. Confirm each destructive step (delete,
overwrite) before running it, per the repo's approval rules.
