# Reforger Manager — Sprint 3 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-03 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: complete

All nine stories (S1–S9) implemented. Backend test suite green
(**215 passed**, baseline 185 → +30 for this feature); frontend `npm run build`
green. The feature was then exercised end-to-end against a freshly rebuilt local
Docker stack (see *Live verification* below).

## Story-by-story

| # | Story | State | Notes |
|---|---|---|---|
| S1 | `servers/files.py` — safe path resolution + listing | done | `resolve_safe_path()` rejects textually **before** `resolve(strict=False)` (NUL, absolute / drive / UNC, any `..` segment, an `EXCLUDED_TOP_LEVEL` prefix), then re-checks `profiles_root ⊃ profile_dir ⊃ candidate` ancestry, then walks every path component and refuses if any is a symlink (inside-pointing included). `_is_relative_to` reused from `logview` (not forked). `FileEntry` frozen dataclass; `is_text` = ≤ `files_max_edit_bytes` + 8 KiB sniff (false on NUL / `UnicodeDecodeError`); `is_json` gated behind `is_text` so the whole-file `json.loads` never runs on an oversized file. `list_dir` non-recursive, `[]` on a missing dir, dirs-first case-insensitive sort, `logs/` + `addons_tmp/` dropped at root, symlinks listed (type `symlink`, `size 0`). `build_archive_bytes` = in-memory `ZIP_DEFLATED`, `os.walk(followlinks=False)`, symlinks + excluded dirs skipped, valid empty zip when the dir is absent. Settings `files_max_edit_bytes` (2 MiB) / `files_max_upload_bytes` (50 MB) added + mirrored to `.env.example`. 17 module tests. |
| S2 | Read-side routes | done | `app/schemas/server_files.py` (`FileEntryOut`, `DirListingOut`, `FileContentOut`, plus the S3 request bodies) + `app/api/server_files.py` mounted at `/api/servers/{server_id}/files` after the servers router. Every route runs `_load` (404 on unknown id) before any filesystem work; `UnsafePath` → 400. `GET /` (empty list, not 404, on a never-started definition; 400 if the path is a file), `GET /content` (200 marker `editable:false` for binary / over-cap, not an error), `GET /download` (`Response(bytes, media_type="application/octet-stream")`, filename sanitised), `GET /archive` (`server-{id}-profile.zip`). 7 route tests. |
| S3 | Write-side routes + running-server 409s | done | `_refuse_while_running()` (`supervisor.active_server_id == server_id and supervisor.is_running()`) on all five mutating routes. `PUT /content` (400 if parent dir missing; server-side `json.loads` for `.json`; atomic temp-file + `os.replace`), `DELETE /` (recursive `rmtree` for dirs, re-resolve immediately before, 400 on the profile root), `POST /mkdir` (`parents=True, exist_ok=True`), `POST /rename` (both ends resolved; 400 on dest-exists / root / move-into-self), `POST /upload` (multipart, streamed 1 MiB chunks, 413 at `files_max_upload_bytes` with partial cleanup, filename `..` rejected). 6 more route tests (the 5-refused / 4-allowed 409 matrix + happy/oversize/traversal/recursive-delete/rename-collision). |
| S4 | Frontend types + transport helpers | done | `frontend/src/lib/api.ts`: `FileEntry` / `DirListing` / `FileContent` types; `apiUpload<T>()` (FormData, field name `files`, no hand-set `Content-Type`), `apiBlob()`, `saveBlob()` — each raw fetch sets `Authorization: Bearer …` and dispatches `auth:expired` on 401, matching `api()`. |
| S5 | Files tab shell + directory browser | done | `"Files"` tab added to `ServerDetail.tsx` after `"Mods"`. New `FilesPanel.tsx`: `useQuery(["server-files", id, path])`, breadcrumb (`profile/` root crumb), dirs-first table (type tag, humanised size, locale mtime, per-row actions cell), amber running banner, `Empty` states (never-started vs empty folder), collapsed `<details>` "Generated config.json (read-only)" sourced from the existing `GET /api/servers/{id}/config`. |
| S6 | File editor dialog + JSON gate | done | Per-row **Open** (only `is_text` files) → `GET /content`; an `editable:false` response shows an inline note and offers Download instead of opening. `Dialog` with a monospace textarea, seeded once at open. When the file is JSON, `JSON.parse` runs every keystroke, the error renders under the textarea and **Save** is disabled. Save → `PUT /content` via `api()`, then prefix-invalidate; a server-side 400/409 surfaces in the dialog. Per-row **Download** (`apiBlob`+`saveBlob`); symlink rows get no actions. Header **Download all as .zip** → `/archive`, stays enabled while running. |
| S7 | Mutating controls | done | **New Folder** (`Dialog` + `Input` → `POST /mkdir`), **Upload** (hidden `<input type=file multiple>` → `apiUpload`, per-file error incl. 413 to `actionError`, input `value` reset), **Rename** (per-row, files + dirs, `Dialog` prefilled → `POST /rename`), **Delete** (per-row, two-click confirm à la `confirmDelete`, dir copy names the folder and warns the contents go with it → `DELETE /`). All disabled while `server.is_running`; every mutation prefix-invalidates `["server-files", id]`. |
| S8 | Documentation | done | `.env.example` `FILES_MAX_EDIT_BYTES` / `FILES_MAX_UPLOAD_BYTES` (match `core/config.py`). `README.md` new *Profile files* subsection (what the tab reaches / deliberately does not, the stop-to-edit rule, the no-backup caveat). `CLAUDE.md` Gotchas bullet: profile files are editable via the UI but `CONFIGS_DIR/{id}.json` is regenerated on every start. |
| S9 | Live verification | done | See below. |

## Data-model changes

**None.** Pure filesystem I/O against the existing `PROFILES_DIR/{server_id}/`
bind mount. No new tables, no columns, no migration.

## New API surface

`GET /api/servers/{id}/files` · `GET .../files/content` · `PUT .../files/content` ·
`DELETE .../files` · `POST .../files/mkdir` · `POST .../files/rename` ·
`POST .../files/upload` · `GET .../files/download` · `GET .../files/archive`.

New frontend deps: **none**.

## Verification performed

- **Backend:** `backend/.venv/Scripts/python -m pytest -q` → **215 passed,
  3 subtests** (baseline 185). Covers every PLAN *Testing* case: `../../etc/passwd`
  / absolute / drive-letter / bare `..` / `foo/../bar` / embedded NUL rejection,
  a symlink pointing outside **and** one pointing inside the profile dir (both
  refused), per-`server_id` scoping (7 vs 8 never cross), `logs/console.log`
  refused, missing profile dir → `[]`, `is_json` false for a broken `.json`,
  `is_text` false on NUL bytes, the full 5-refused / 4-allowed 409 matrix,
  server-side JSON gate on `PUT`, size-cap `editable:false` fallback still
  reachable via `/download`, archive round-trip with the symlink skipped,
  multipart happy-path + 413 + `../` filename → 400.
- **Frontend:** `npm run build` (`tsc -b && vite build`) — green, no TS errors.

## Live verification (S9)

The local stack was rebuilt (`docker compose up -d --build`) with the sprint
code and driven through the browser (chrome-devtools) plus the raw API.

**Test fixture.** Rather than the multi-GB JLH engine install (out of scope for
this pass — no engine binary is downloaded until an install is triggered), a
definition (`#12`, "JLH Conflict PvE (S3 test)") was created via
`POST /api/servers` and its `PROFILES_DIR/12/` was seeded — in the container, as
the `steam` user — with a tree mirroring the persistence layouts PLAN *Context*
identified:

```
PersistentXP.json            – single JSON at the profile root      (PersistentXP)
GMPersistentLoadouts/        – subfolder of per-loadout JSON        (GM Persistent Loadouts)
  us_rifleman.json
  broken.json                – .json suffix, deliberately invalid JSON
gmp_save_arland.json         – one JSON per map                     (GM Persistence)
save/                        – engine built-in Persistence System
  meta-info.json             –   the index
  9f2c/session.bin           –   a UUID-keyed binary session blob
README.txt                   – non-JSON text
big/huge.json                – 3 MB, over files_max_edit_bytes
.backup/                     – a dot-dir
logs/console.log             – must be hidden by the browser
addons_tmp/xyz/…             – must be hidden by the browser
evil_link      -> /etc/hostname          (symlink escaping the profile dir)
inside_link.json -> PersistentXP.json    (symlink inside the profile dir)
```

**Findings — real-world data point on mod persistence.** Consistent with PLAN
*Context*: there is **no single convention**. Files are flat under the profile
root or one level down; names are per-mod and not always predictable; some are
JSON, some (the engine's own `save/<uuid>/…` blobs) are not. A generic path-safe
CRUD browser with a JSON gate keyed on *actual parseability* — not on the `.json`
extension — is the right shape, and is what shipped. `save/` (the engine
Persistence System, governed by the `persistence` block already reachable via
`extra_config`) is **not** excluded from the browser and showed up correctly as a
normal folder of mixed JSON/binary files.

**Exercised through the UI, all passing:**

| Check | Result |
|---|---|
| Files tab lists the root; dirs first, then files | ✅ |
| `logs/` and `addons_tmp/` absent from every listing | ✅ |
| Symlinks (`evil_link`, `inside_link.json`) listed as `LINK`, **no action buttons** | ✅ |
| Breadcrumb navigation into `GMPersistentLoadouts/` and back | ✅ |
| Open `PersistentXP.json` → editor seeded with file content | ✅ |
| Type invalid JSON → inline parse error, **Save disabled** | ✅ |
| Fix JSON, Save → dialog closes, list refreshes, size/mtime update, **change on disk via bind mount** | ✅ |
| Open already-broken `broken.json` (`.json`, unparseable) → opens as text, Save enabled, but server-side `PUT` returns `400 invalid JSON: …` shown in the dialog, file unchanged | ✅ (server-side gate is authoritative) |
| Collapsed "Generated config.json (read-only)" panel renders the live config | ✅ |
| New Folder `archive_2026` inside a subdir → appears immediately | ✅ |
| Delete folder → two-click confirm, dir-specific copy ("…/ and everything in it?"), removes it | ✅ |
| Console clean — the only error is the intentional 400 above | ✅ |

**Exercised via the raw API (chrome file-input drag is awkward to script):**

| Check | Result |
|---|---|
| `GET /content` on the 3 MB `.json` and on `save/9f2c/session.bin` → `editable:false, content:null` (200 marker, not an error); both still pull via `/download` | ✅ |
| `GET /archive` → 8 entries, **both symlinks skipped**, `logs/` + `addons_tmp/` excluded, every other file byte-identical | ✅ |
| `GET /?path=../..` → 400; `GET /content?path=evil_link` → 400 ("path escapes the profile directory"); `GET /?path=logs` → 400 ("not browsable through this API") | ✅ |
| `PUT` new file under a missing parent → 400 ("create it first"); `mkdir` then `PUT` into it → 200 | ✅ |
| `POST /rename` onto an existing name → 400; `DELETE /?path=` (root) → 400 ("cannot delete the profile root") | ✅ |
| `POST /upload` happy path → file written; oversized → 413 with partial cleanup; `filename=../../evil.txt` → 400, nothing written outside the profile dir (httpx-driven unit test; `curl` mangles such a filename before it leaves the client) | ✅ |

## Not exercised

- **The JLH serverpack §9 functional checklist (QRF / RECAP factions in-game).**
  It needs the actual engine install + the RHS / WCS / MrSaint modpack downloaded
  and a live game client — a multi-GB, multi-step operation outside this
  verification pass. Every *Files-tab* capability that checklist depends on
  (browse, open, edit JSON, upload, rename, delete, zip, all without touching the
  NAS by hand) is verified above against a representative seeded tree.
- **The running-server 409s in the browser.** Definition `#12` was never started
  (no engine), so `server.is_running` stayed false and the disabled-state / amber
  banner could not be shown live. The backend 409 matrix (all five mutating
  routes refused, all four read routes allowed, with `supervisor` patched to
  "running") is asserted in `tests/test_server_files_routes.py`.

## Minor observations (no code change made)

- A `.json` file that is **already** invalid on disk opens in the editor with
  **Save enabled** (the client gate keys off `is_json`, which is false when the
  fetched content doesn't parse). Saving it unchanged is then rejected by the
  server-side gate with the parse error surfaced in the dialog — so the file is
  never corrupted further, but the client could pre-empt the round-trip. Left as
  is: the server gate is the guarantee, and being able to open-and-fix a
  broken file is the desired flow.

## How this sprint was implemented

Per `CLAUDE.md` ("Implementing a sprint"): every story delegated to sub agents,
rotated evenly across the native Sonnet agent and the two `opencode` models
(`deepseek-v4-flash`, `glm-5.3-flash`) — S1/S4/S7 native, S2/S5/S8 deepseek,
S3/S6 glm — with the orchestrator distilling each story's PLAN/STORIES context
into a self-contained brief so the sub agents never opened the sprint docs.
Dependency order was serialised where stories shared a file (`server_files.py`:
S2 then S3; `FilesPanel.tsx`: S5 → S6 → S7); S8 (docs) ran alongside S2. One
deepseek run stalled after planning without writing files and was restarted with
an action-first brief. Each story was verified (`pytest` + `npm run build`)
before the next dependent story started. S9 (this verification) was run by the
orchestrator against the rebuilt local stack.
