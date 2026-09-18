# Reforger Manager — Sprint 12 implementation stories

> Context, decisions, and the file-level rationale live in [PLAN.md](PLAN.md).
> References below use `file:line` — trust the symbol name over the line number if
> it has drifted during the sprint.

## Shape of the sprint

Ten stories in four phases. Phase 1 is the one hard dependency everything else
needs (the graph endpoint's response shape and the client-side graph-walk
functions); once that contract is locked, phases 2–3 fan out over five
independent files and can run fully in parallel.

| Phase | Stories | Theme |
|---|---|---|
| 1 | S1–S2 | Backend graph endpoint + frontend graph-walk library |
| 2 | S3–S5 | Shared UI components (`ModTree`, `ModLibraryPicker`, `FreshnessPill`) |
| 3 | S6–S9 | The four consuming surfaces (Mod Library, Server Mods, Modpack Editor, Mod Detail) |
| 4 | S10 | Cleanup — delete `modCoverage.ts`, final build sweep |

**Parallelism:**
- S1 and S2 touch disjoint files (backend vs. frontend) and S2's contract is fully
  specified below — start both together.
- S3, S4, S5 are three new files with no overlap — parallel, once S2 lands (they
  import `useModGraph`/`buildNested`/etc.).
- S6, S7, S8, S9 each own one existing page/component file — parallel, once S3–S5
  land. **Do not** let two of these run against the same file simultaneously; they
  don't share one, so this is naturally safe.
- S10 is last: it only runs once S6–S9 have all removed their `modCoverage`
  imports.

**A scope note that overrides one PLAN.md sentence:** PLAN.md's Server Mods /
Modpack Editor sections describe the "Added" pane as adopting `ModTree` wholesale.
Having read the actual components, that pane also does drag-to-reorder
(`SortableModList`, `dnd-kit`), inline pin/unpin, enable/disable, and a working-copy
diff model — none of which PLAN.md's nested/flat framing accounts for, and none of
which this sprint is meant to touch (PLAN.md's non-goals never mention removing
DnD or pins). S7/S8 below thread the needle: the **Flat** toggle state keeps
`SortableModList` exactly as it is today (just re-sourced from the graph instead
of `useModCoverage`'s per-item fetches), and the **Nested** toggle state swaps in
a read-only `ModTree` (no drag, no pin controls — those stay in Flat). This keeps
every existing mutation, pin round-trip, and DnD interaction byte-identical while
still delivering PLAN's actual ask: one shared graph fetch instead of N
per-pick dependency-tree requests, and a nested view that didn't exist before.

---

## Shared reference (read before starting any story)

| Thing | Where | Notes |
|---|---|---|
| `Mod` model | `backend/app/models/mod.py:28` | `guid`, `name`, `is_local`, `is_unlisted/private/obsolete`, `api_state` (enum, `.value` is the string), `api_checked_at`. |
| `ModDependency` model | `backend/app/models/mod.py:77` | `mod_guid`, `depends_on_guid`, `depends_on_name`, `source` (`"api"` \| `"gproj"` \| `None`). Not an FK on `depends_on_guid` — it can dangle. |
| `ENGINE_BUILTIN_GUIDS` | `backend/app/mods/resolve.py:40` | `frozenset({"58D0FB3206B6F859", "5614BBCCBB55ED1C"})`. These two guids appear as `depends_on_guid` values but never get a `mods` row (`backend/app/mods/sync.py:223` skips stub-row creation for them). |
| Mods router | `backend/app/api/mods.py:63` (`router`), routes registered in file order; `/search` (`:235`) is placed before `/{guid}` (`:423`) specifically to avoid path-shadowing — **the new `/graph` route must go in the same "before `/{guid}`" block**. |
| Mod schemas | `backend/app/schemas/mod.py` | `ModOut`, `ResolvedTreeOut`/`ResolvedNodeOut` (existing per-mod tree, untouched), `ModRefOut`. |
| Existing dependency test conventions | `backend/tests/test_mod_dependents.py` | In-memory sqlite fixture (`session` pytest_asyncio fixture), calls router functions directly (`await mods_api.list_mods(...)`) rather than going through an HTTP client. Follow this shape for the new test file. |
| Frontend API client | `frontend/src/lib/api.ts:243` (`api<T>`), `:321` (`apiClient`), `:67` (`Job` type: `id, kind, state, progress, current_step, error, created_at`) | `state` values: `"queued" \| "running" \| "succeeded" \| "failed" \| "cancelled"` (terminal set in `frontend/src/pages/Jobs.tsx:20`). |
| `useModCoverage` (being replaced) | `frontend/src/lib/modCoverage.ts` | One `useQueries` fetching `["mod", guid, "deps"]` per explicit pick. Its return shape — `{ coverageByParent: Map<guid, ReadonlySet<guid>>, covered: ReadonlySet<guid> }` — is preserved by the replacement so S7/S8's consuming JSX barely changes. |
| `SortableModList` | `frontend/src/components/mods/SortableModList.tsx` | Generic DnD list over `SortableRow = {key, guid, name}`. `renderExpanded` slot is where `AssignedModExpanded`/`PackItemExpanded` render today — S7/S8 replace *only* what's rendered in that slot, not the list itself. |
| `ui.tsx` primitives | `frontend/src/components/ui.tsx` | `Button`, `Card`/`CardHeader`/`CardTitle`/`CardContent`, `Input`, `Badge` (`tone: "good"|"warn"|"bad"|"neutral"`), `Dialog`. No table/tree primitive exists — house style for lists is `<ul className="divide-y divide-stone-800">` / bordered `<li>` rows, `font-display uppercase tracking-wide text-stone-100` for names, `font-mono text-[10px] text-stone-500` for guids. Match this, don't invent a new visual language. |
| Job kind for mod scan | `backend/app/api/mods.py:65` (`JOB_KIND_MOD_SYNC = "mod_sync"`), enqueued at `:494` | Frontend already POSTs `/api/mods/scan` and gets back `{job_id, kind}` (`frontend/src/pages/Mods.tsx:47` `EnqueuedJob`). |

---

## Phase 1 — Backend graph endpoint + frontend graph-walk library

### S1 — `GET /api/mods/graph` · backend

**Deliver.** In `backend/app/schemas/mod.py`, add:

```python
class ModGraphNodeOut(BaseModel):
    guid: str
    name: str | None = None
    is_local: bool = False
    api_state: str = "unchecked"
    is_unlisted: bool = False
    is_private: bool = False
    is_obsolete: bool = False
    api_checked_at: datetime | None = None
    is_builtin: bool = False


class ModGraphOut(BaseModel):
    nodes: list[ModGraphNodeOut]
    edges: list[dict]   # [{"from": guid, "to": guid, "source": "api"|"gproj"|None}, ...]
```

(`edges` as plain dicts matches the existing `ResolvedTreeOut.edges` convention at
`backend/app/schemas/mod.py` — don't build a pydantic model for the edge, the
codebase already made that call for the sibling endpoint.)

In `backend/app/api/mods.py`, add a route **between `search_mods` (`:235-255`) and
`check_all_updates` (`:260`)** — i.e. still before `/{guid}` — one query against
`Mod`, one against `ModDependency`, joined in Python, zero Workshop calls:

```python
_BUILTIN_NODE_NAMES = {
    "58D0FB3206B6F859": "Arma Reforger (base data)",
    "5614BBCCBB55ED1C": "Core",
}


@router.get("/graph", response_model=ModGraphOut)
async def get_mods_graph(session: AsyncSession = Depends(get_session)) -> ModGraphOut:
    mods = (await session.execute(select(Mod))).scalars().all()
    nodes: dict[str, ModGraphNodeOut] = {
        m.guid: ModGraphNodeOut(
            guid=m.guid,
            name=m.name,
            is_local=m.is_local,
            api_state=m.api_state.value if m.api_state else "unchecked",
            is_unlisted=m.is_unlisted,
            is_private=m.is_private,
            is_obsolete=m.is_obsolete,
            api_checked_at=m.api_checked_at,
        )
        for m in mods
    }

    dep_rows = (
        await session.execute(
            select(
                ModDependency.mod_guid,
                ModDependency.depends_on_guid,
                ModDependency.depends_on_name,
                ModDependency.source,
            )
        )
    ).all()

    edges: list[dict] = []
    for mod_guid, dep_guid, dep_name, source in dep_rows:
        edges.append({"from": mod_guid, "to": dep_guid, "source": source})
        if dep_guid not in nodes and dep_guid in ENGINE_BUILTIN_GUIDS:
            nodes[dep_guid] = ModGraphNodeOut(
                guid=dep_guid,
                name=dep_name or _BUILTIN_NODE_NAMES.get(dep_guid, dep_guid),
                is_local=True,
                api_state="ok",
                is_builtin=True,
            )

    return ModGraphOut(nodes=list(nodes.values()), edges=edges)
```

Add `ENGINE_BUILTIN_GUIDS` to the existing `from ..mods.resolve import
resolve_dependencies` import line (`backend/app/api/mods.py:38`), and
`ModGraphNodeOut, ModGraphOut` to the `from ..schemas.mod import (...)` block
(`:48-59`).

**Done when.**
- `backend/tests/test_mods_graph.py` (new) covers, using the same in-memory-sqlite
  `session` fixture pattern as `test_mod_dependents.py`:
  1. A library of 3 `Mod` rows + 2 `ModDependency` edges — the response's `nodes`
     contains all 3 guids and `edges` contains both edges verbatim (`from`/`to`/
     `source`).
  2. A `ModDependency` row whose `depends_on_guid` is `58D0FB3206B6F859`
     (`ENGINE_BUILTIN_GUIDS` member) with no corresponding `Mod` row — the
     response still includes a node for it, with `is_builtin=True`.
  3. A `ModDependency` row whose `depends_on_guid` is neither in `mods` nor in
     `ENGINE_BUILTIN_GUIDS` (an unresolved dependency) — the response's `edges`
     still includes it, but `nodes` does **not** gain an entry for it (this is
     the "dangling = unresolved" contract S2's `buildNested`/`buildFlat` rely on
     — don't "fix" it by synthesizing a node).
  4. Monkeypatch `app.mods.workshop.workshop` (the module-level singleton
     `backend/app/mods/workshop.py`) so every method raises `AssertionError` if
     called, then call `mods_api.get_mods_graph(session=session)` directly and
     assert it doesn't raise — proves the route never touches the Workshop
     client.
  5. Assert the serialized node dict has no `size`/`tags`/`thumbnail`/`versions`
     keys (`ModGraphNodeOut` doesn't declare them, so this is really "the schema
     stayed minimal" — a `model_dump()` key-set assertion is enough).
- `cd backend && .venv/Scripts/python.exe -m pytest -q` passes, no regressions in
  the rest of `backend/tests/`.

**Watch out.** `Mod.api_state` is an `ApiState` enum (`backend/app/models/base.py:35`)
— read `.value`, don't pass the enum member straight into a `str`-typed pydantic
field (mirrors the existing `_to_out` pattern... actually `_to_out` at
`backend/app/api/mods.py:89` relies on `ModOut.model_validate(mod)` doing the enum
coercion implicitly via `ConfigDict(from_attributes=True)` — this new endpoint
builds `ModGraphNodeOut` by hand, field by field, so it needs the explicit
`.value` or pydantic will serialize the enum repr, not the string).

---

### S2 — `frontend/src/lib/modGraph.ts` (new)

**Deliver.** A new file, **not yet wired into any page** (that's S6–S9). Shape:

```ts
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { api, type Job } from "./api";

export type ModGraphNode = {
  guid: string;
  name: string | null;
  is_local: boolean;
  api_state: "ok" | "not_found" | "unchecked";
  is_unlisted: boolean;
  is_private: boolean;
  is_obsolete: boolean;
  api_checked_at: string | null;
  is_builtin: boolean;
};
export type ModGraphEdge = { from: string; to: string; source: string | null };
export type ModGraph = { nodes: ModGraphNode[]; edges: ModGraphEdge[] };

export type TreeNode = {
  guid: string;
  node: ModGraphNode | null;   // null => dangling/unresolved guid, no mods row
  depth: number;
  children: TreeNode[];
};

export function useModGraph() {
  return useQuery({
    queryKey: ["mods-graph"],
    queryFn: () => api<ModGraph>("/api/mods/graph"),
    staleTime: 5 * 60_000,
  });
}

/** Poll `GET /api/jobs/{jobId}` until it reaches a terminal state, then
 *  invalidate `["mods-graph"]` once. Pass `null` to disable. */
export function useInvalidateGraphOnJob(jobId: number | null) {
  const queryClient = useQueryClient();
  const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
  useQuery({
    queryKey: ["mods-graph-watch", jobId],
    queryFn: async () => {
      const job = await api<Job>(`/api/jobs/${jobId}`);
      if (TERMINAL.has(job.state)) {
        void queryClient.invalidateQueries({ queryKey: ["mods-graph"] });
      }
      return job;
    },
    enabled: jobId !== null,
    refetchInterval: (query) => (query.state.data && TERMINAL.has(query.state.data.state) ? false : 2000),
  });
}

export function buildNested(roots: string[], graph: ModGraph): TreeNode[] { /* ... */ }
export function buildFlat(roots: string[], graph: ModGraph): ReadonlySet<string> { /* ... */ }
export function buildReverse(root: string, graph: ModGraph, mode: "nested"): TreeNode[];
export function buildReverse(root: string, graph: ModGraph, mode: "flat"): ReadonlySet<string>;
export function freshnessOf(guids: string[], graph: ModGraph): string | null { /* oldest api_checked_at, or null if any guid has none */ }

/** Back-compat shape for S7/S8: same as the old useModCoverage return. */
export function coverageFromGraph(
  roots: string[],
  graph: ModGraph | undefined,
): { coverageByParent: Map<string, ReadonlySet<string>>; covered: ReadonlySet<string> } { /* ... */ }
```

Implementation notes (this is the one piece of real logic in the sprint):

- **`buildNested(roots, graph)`** — for each root, DFS via a `Map<guid, Edge[]>`
  adjacency built once from `graph.edges` (`from -> [{to, source}]`). A node can
  legitimately appear more than once in the same render (once per parent that
  reaches it, and again as its own root if it's independently in `roots`) — do
  **not** dedupe across siblings, only guard the single root-to-leaf path with a
  `Set<guid>` (an ancestor chain) so a cycle can't infinite-loop. Cap depth at 12
  to match the backend's `_MAX_DEPTH` (`backend/app/mods/resolve.py:33`) as a
  belt-and-braces guard, not because cycles are expected. `node: null` for a
  `to` guid that has no entry in `graph.nodes`.
- **`buildFlat(roots, graph)`** — full reachable closure from all roots, deduped
  into one `Set<guid>` (roots excluded — this mirrors `useModCoverage`'s
  `depth > 0` filter).
- **`buildReverse`** — same two shapes, walking `to -> from` (an edge whose `to`
  is the queried guid means its `from` "requires" it, i.e. depends on it — so
  reverse-from-`to` gives "what requires this guid").
- **`freshnessOf`** — `Math.min` over parsed `api_checked_at` timestamps of the
  given guids' nodes (skip guids with `node === null` or `api_checked_at ===
  null`); return `null` if the set is empty after that filter, not `0`/`NaN`.
- **`coverageFromGraph`** — for each `guid` in `roots`, `coverageByParent.set(guid,
  buildFlat([guid], graph))`; `covered` is the union of all those sets, minus
  `roots` itself (a root that's *also* someone else's dependency should still be
  hoisted the way `useModCoverage` did it — check `test_mod_dependents.py`-style
  reasoning: this must produce the same `covered` set `useModCoverage` would have
  for the same `roots` + edge data, just from one fetch instead of N).

**Done when.** `cd frontend && npm run build` passes. No test harness exists for
frontend (CLAUDE.md) — this story is build-clean only; S6–S9 exercise the logic
for real once wired in.

**Watch out.** `useModCoverage`'s `covered` set only ever contained *other
explicit picks* reachable through a pick's closure — never the pick itself, even
if some cycle made a guid reach itself. `coverageFromGraph` must preserve that:
subtract `roots` from the union before returning `covered`.

---

## Phase 2 — Shared components (`frontend/src/components/mods/`, new directory)

All three of S3–S5 import only from `../../lib/modGraph` and `../ui` — no
cross-imports between them, so they're safe to run at the same time.

### S3 — `ModTree.tsx`

**Deliver.** Renders either `TreeNode[]` (nested) or a `ReadonlySet<string>` +
`ModGraph` (flat) — the caller picks which by which prop it passes, this
component does not own the Nested/Flat toggle state itself (S6–S9 each own
that toggle, since each surface's toggle sits next to different sibling
controls).

```ts
type ModTreeProps = {
  graph: ModGraph;
  mode: "nested" | "flat";
  nested?: TreeNode[];   // required when mode === "nested"
  flat?: ReadonlySet<string>;   // required when mode === "flat"
  onGuidClick?: (guid: string) => void;   // default: none (plain text, not a link)
  linkTo?: (guid: string) => string;   // when given, guid renders as a <Link to={linkTo(guid)}>
  highlightGuids?: ReadonlySet<string>;   // brief highlight class, see below
};
```

- Nested mode: recursive `<ul>`/`<li>`, indent via `style={{ paddingLeft:
  \`${Math.min(depth, 8) * 1.25}rem\` }}` (copy the existing convention from
  `frontend/src/pages/ModDetail.tsx:337`). A node with `node === null` renders as
  `<span className="font-mono text-stone-500">{guid}</span>` plus
  `<Badge tone="warn">unresolved</Badge>` — no link, mirroring
  `ModDetail.tsx:339-349`'s `state !== "unresolved"` branch, just keyed off
  `node === null` instead of a `state` string (the graph payload has no `via`/
  `state` per edge — `api_state` on the node is the closest analog; render it as
  `<Badge tone={...}>{node.api_state}</Badge>` when not builtin/unresolved).
- Flat mode: same row rendering, no indentation, one `<ul>`, iterate
  `[...flat].sort()` resolved against `graph.nodes` (a guid in `flat` with no
  graph node is still "unresolved" — flat mode can reach dangling guids too).
- `is_builtin` nodes (`ENGINE_BUILTIN_GUIDS`) render with `<Badge
  tone="neutral">engine</Badge>` and are never clickable (no Workshop page to
  link to) — this is the client-side "hide/flag" PLAN.md asks for; don't filter
  them out of the tree, just mark them (pre-flight already surfaces them this
  way elsewhere per PLAN.md's "reuse the constant, don't re-derive").
- `highlightGuids` — apply a `ring-1 ring-amber-400` (or similar — match the
  existing drag-highlight convention at
  `frontend/src/components/mods/SortableModList.tsx:129`) class to any row whose
  guid is in the set, for one render pass. S6 is the only consumer that computes
  this (comparing two successive graph fetches) — S3 just needs to accept and
  apply the prop, not compute it.

**Done when.** `npm run build` passes. Not wired anywhere yet.

**Watch out.** Don't give tree `<li>` elements React keys that collide across
depths — a guid can appear at multiple depths in nested mode (that's the whole
point), so key on `` `${guid}-${depth}-${parentGuid}` `` or an index path, not
bare `guid` (mirrors the existing `key={`${node.guid}-${node.depth}`}` at
`ModsPanel.tsx:1201`, but that alone isn't enough since two different parents at
the same depth can both list the same child — include a parent/path segment).

---

### S4 — `ModLibraryPicker.tsx`

**Deliver.** Consolidates `AddModRow` + its container
(`frontend/src/components/server/ModsPanel.tsx:1033-1097` and the surrounding
picker JSX at `:768-829`) and Modpacks' inline equivalent
(`frontend/src/pages/Modpacks.tsx:438-482`, no separate row component there —
it's inline `<li>`s). Modpacks' version has no per-row deps-preview toggle or
pagination; ModsPanel's does. Build the picker to support both by making the
richer behavior optional:

```ts
type ModLibraryPickerProps = {
  disabledGuids: ReadonlySet<string>;   // explicit picks ∪ their resolved closure — greyed, not hidden
  onAdd: (mod: ModRecord) => void;   // ModRecord = the existing `Mod` type from lib/api.ts
  showDepsPreview?: boolean;   // default true (ModsPanel's AddModRow behavior); Modpacks passes false
  pageSize?: number;   // default 10 (ModsPanel's current page size); Modpacks passes Infinity (no paging today)
};
```

Internally: owns `modSearch`/`showNonLocal`/`page` state, the `useQuery(["mods",
"library", showNonLocal], ...)` fetch (identical query key/fn to
`ModsPanel.tsx:383-386` and `Modpacks.tsx:88-90` — keep the key so both call
sites share cache), and the candidate filter — but candidates are no longer
*excluded* when in `disabledGuids` (today's `ModsPanel.tsx:391` does `.filter((mod)
=> !workingGuids.has(mod.guid))`, i.e. hides). Per PLAN.md's manual-test item 3
("bottom pane greys out mods already covered"), render every candidate and grey
out + disable the `Add` button for guids in `disabledGuids` (`opacity-50
pointer-events-none` on the row, or a disabled `Button`), rather than filtering
them out of the list. Keep the search/non-local filters as real filters (they
still exclude, per existing behavior) — only the disabled-set changes from
"excluded" to "greyed."

**Done when.** `npm run build` passes. Not wired anywhere yet.

**Watch out.** `ModsPanel.tsx`'s current `candidates` computation caps at `.slice(0,
60)` before paginating — keep that cap (it's an existing, deliberate guard, not
an oversight) when `showDepsPreview`/paging is on; Modpacks' call site (no
paging) should probably keep its own current unbounded-but-search-filtered
behavior — pass a prop rather than silently changing either surface's existing
cap.

---

### S5 — `FreshnessPill.tsx`

**Deliver.**

```ts
type FreshnessPillProps = {
  timestamp: string | null;   // from freshnessOf()
  syncing?: boolean;   // true while a mod_sync job is active for this view
};
```

`syncing` → `<Badge tone="warn">syncing…</Badge>`. Otherwise: `timestamp === null`
→ `<Badge tone="neutral">never synced</Badge>`; else a relative-time string
("synced 4m ago" / "synced 2h ago" / "synced 3d ago"). Reuse the exact relative-time
bucketing already written at `frontend/src/pages/Jobs.tsx:31-42` (`relTime`) rather
than re-deriving rounding rules — copy the function (small, no shared module to
import it from without creating a new cross-page dependency; a 12-line duplicate
is fine here per CLAUDE.md's "three similar lines beat a premature abstraction").

**Done when.** `npm run build` passes. Not wired anywhere yet.

**Watch out.** None of note — this is the smallest story in the sprint.

---

## Phase 3 — Consuming surfaces

Each of S6–S9 owns exactly one file. Read the "scope note" at the top of this
document before starting S7 or S8 — you are **not** rewriting DnD, pins, or the
working-copy save flow, only the coverage-computation source and the
add-from-library / expanded-subtree pieces.

### S6 — Mod Library (`frontend/src/pages/Mods.tsx`)

**Deliver.**
- Add a Nested/Flat toggle (two `Button`s, `variant="outline"` for the inactive
  one, matching the toolbar button style already used at `Mods.tsx:399-410`) next
  to the existing filter row.
- `useModGraph()` fetch. Nested roots = every `ModRecord` in the existing
  `modsQuery` result (`Mods.tsx:190-193`) with `is_local === true` — render via
  `ModTree` with `linkTo={(guid) => `/mods/${guid}`}`.
- Flat: keep the existing table (`COLUMNS`/`sortRows`/the `<table>` JSX) exactly
  as-is — PLAN.md says flat "existing table view, unchanged in spirit."
- Filters (Local only / Has update / Orphans / Not resolved — the existing
  `local`/`state`/`updatesOnly` state at `Mods.tsx:168-170`, plus the
  `is_orphan`/`is_unreferenced` fields already on `ModRecord`) apply to flat as
  today (server-side query params, unchanged). For nested, apply them
  client-side over the graph-derived roots: a root that fails the filter is
  dropped **only if none of its descendants pass** (PLAN.md: "filtering hides
  non-matching leaves but keeps ancestors needed to reach a matching
  descendant"). Since the graph endpoint carries no `has_update`/`is_orphan`
  fields (those are computed server-side in `_to_out`/`list_mods` from
  `modsQuery`, not present on `ModGraphNode`), join by guid against the existing
  `modsQuery.data` to get those computed flags for the nested-filter check —
  don't duplicate that computation against graph nodes.
- `FreshnessPill` next to the Nested/Flat toggle: `freshnessOf(nestedRootGuids,
  graph)`, `syncing` = true while the last-triggered scan job (see below) hasn't
  reached a terminal state.
- Wire the existing "Rescan" button (`Mods.tsx:399`,
  `jobMutation.mutate({ path: "/api/mods/scan" })`) to also track the returned
  `job_id` in a new `const [scanJobId, setScanJobId] = useState<number | null>(null)`,
  set from `jobMutation.onSuccess`, and call `useInvalidateGraphOnJob(scanJobId)`
  from `modGraph.ts` (S2). This satisfies PLAN's "invalidate on mod_sync job
  completion."
- Highlight-on-change (PLAN.md decision 5): keep the previous render's
  `buildNested` output in a `useRef`, diff parent-sets per guid against the new
  one on each graph refetch, pass the changed guids as `ModTree`'s
  `highlightGuids` for one render, then clear.

**Done when.** `cd frontend && npm run build` passes. Manual check (Phase E, not
this story): nested shows only `is_local` mods as roots with deps nested beneath;
flat is unchanged from today.

**Watch out.** `modsQuery`'s queryKey includes `filterString` (`Mods.tsx:191`) so
switching filters re-fetches the *flat* list from the server — don't let the
Nested/Flat toggle itself trigger a `modsQuery` refetch; nested reads from the
one shared `useModGraph()` cache plus whatever `modsQuery.data` already holds for
the flag join above (fetch it unconditionally, not `enabled: !nested`, so
toggling doesn't cause a loading flash).

---

### S7 — Server Mods (`frontend/src/components/server/ModsPanel.tsx`)

**Deliver.**
- Replace the `useModCoverage(working)` import/call (`:17`, `:264`) with
  `coverageFromGraph(working.map((m) => m.mod_guid), graph)` from `modGraph.ts`,
  where `const { data: graph } = useModGraph()`. Guard: while `graph` is
  undefined (first load), pass an empty `ModGraph` (`{nodes: [], edges: []}`) so
  `coverageByParent`/`covered` behave like "nothing covered yet" instead of
  throwing — same effective behavior as `useModCoverage`'s per-item loading state
  today.
- Everything downstream of `coverageByParent`/`covered` — `topLevel`,
  `reorderVisible`, the `SortableModList` JSX (`:670-758`), pins, enable/disable,
  save — **unchanged**. This is the "Flat" state of the new toggle; it's today's
  behavior, just re-sourced.
- Add the Nested/Flat toggle next to "Assigned mods (N)" (`:637-638`). Flat =
  current `SortableModList` block verbatim. Nested = a **new, read-only**
  `ModTree` over `buildNested(working.map(m => m.mod_guid), graph)`, `linkTo`
  wired the same as today's row links (`/mods/${guid}`), no drag handle, no
  pin/enable controls (those stay Flat-only per the scope note). Add the
  existing helper text ("Drag the handle to reorder..." at `:762-765`) only in
  Flat mode; Nested gets its own short caption, e.g. "Switch to Flat to reorder
  or edit pins."
- Delete `AssignedModExpanded` (`:1104-1228`) and its `renderExpanded` wiring
  (`:743-757`) — replaced by rendering `ModTree` (nested, rooted at just that
  one parent guid) inline in the same slot, but **keep the pick-badge rows**
  (`:1144-1192`, the "other explicit picks this one covers, with their own
  enable/pin controls") since that's Flat-mode-only functionality `ModTree` was
  never asked to carry (S3 has no pin/enable props). Concretely: `renderExpanded`
  still renders the pick rows exactly as today (unchanged JSX, just fed by the
  new `coverageGuids`), and additionally renders `<ModTree graph={graph}
  mode="nested" nested={buildNested([mod.mod_guid], graph).filter(n => n.depth >
  0)} linkTo={...} />` in place of the old `pureNodes` block (`:1194-1225`).
- Replace the "Add mods from the library" section (`:768-829`) and `AddModRow`
  (`:1033-1097`) with `<ModLibraryPicker disabledGuids={new
  Set([...workingGuids, ...covered])} onAdd={addMod} showDepsPreview pageSize={10}
  />`.

**Done when.** `npm run build` passes.

**Watch out.** `coverageFromGraph`'s `covered` set must match what
`useModCoverage` would have produced for the same `working` — this is the thing
most likely to subtly regress (e.g. Watch out in S2 about `covered` excluding
`roots`). If in doubt, temporarily log both side by side against a real modded
server's data during manual verification (Phase E) rather than trusting the
diff alone.

---

### S8 — Modpack Editor (`frontend/src/pages/Modpacks.tsx`)

**Deliver.** Mirror of S7, same scope-note rules (Flat keeps `SortableModList`/
DnD unchanged, Nested is new and read-only):
- Replace `useModCoverage(items)` (`:9`, `:163`) with `coverageFromGraph(items.map(i
  => i.mod_guid), graph)`.
- Add Nested/Flat toggle next to "Pack contents (N)" (`:389`). Flat = existing
  `SortableModList` block (`:391-429`) unchanged. Nested = read-only `ModTree`
  over `buildNested(items.map(i => i.mod_guid), graph)`.
- `PackItemExpanded` (`:739-...`, the `AssignedModExpanded` analog minus
  enable/pin) — same treatment as S7: keep the covered-picks rows (packs have no
  enable/pin, so this is just a name + "pick" badge + remove), swap its
  dependency-node listing for a `ModTree` nested-at-one-parent render.
- Replace the inline "Add mods from the library" `<li>` list (`:461-478`) with
  `<ModLibraryPicker disabledGuids={new Set([...itemGuids, ...covered])}
  onAdd={addItem} showDepsPreview={false} pageSize={Infinity} />` — Modpacks
  never had a deps-preview toggle or paging, keep it that way (per S4's Watch
  out, don't silently add paging here).

**Done when.** `npm run build` passes.

**Watch out.** Same coverage-parity concern as S7. Also: `itemGuids` is the
Modpacks equivalent of `workingGuids` — confirm the exact variable name at
`Modpacks.tsx` before wiring `disabledGuids` (grep `itemGuids` — it's referenced
at `:106` already).

---

### S9 — Mod Detail (`frontend/src/pages/ModDetail.tsx`)

**Deliver.**
- "Dependency Tree" card (`:324-359`) → rename to "This mod requires" (or keep
  the existing title if `RESULTS.md` review prefers minimal churn — not load-
  bearing). Replace the flat `dependency_tree.nodes` render with a Nested/Flat
  toggle: nested = `ModTree` over `buildNested([guid], graph)`; flat = `ModTree`
  over `buildFlat([guid], graph)`. Drop the `dependency_tree` prop dependency
  for this card entirely — `ModDetailOut.dependency_tree` stays on the backend
  response (PLAN.md: "leave it... keep this field only as a fallback") but the
  frontend no longer reads it here.
- "Required By" card (`:361-389`) → same Nested/Flat toggle, using
  `buildReverse(guid, graph, mode)`. This upgrades it from today's direct-only
  list (`detail.required_by`, sourced from the backend's one-hop query at
  `backend/app/api/mods.py:438-448`) to the full recursive reverse closure per
  PLAN.md ("the full recursive reverse hierarchy... computed client-side from
  `/graph`"). Keep `detail.required_by` for the *delete-guard copy* just below
  it (`:438-442`, "these mods depend on this one — the delete will be refused"),
  since that text describes backend delete-refusal behavior which is still
  driven by the one-hop `required_by` (`backend/app/api/mods.py:153-179`
  `_delete_block_detail` only looks at direct dependents + closure *owners*, not
  a UI-side reverse walk) — don't change that copy's data source, only the card
  above it.
- "Used By" card (`:391-420`) — leave `detail.used_by` (server names) as-is; add
  the direct-vs-indirect tag PLAN.md asks for by cross-referencing each
  server's own mod list. This needs each server's explicit `ServerMod` guids,
  which `ModDetail.tsx` doesn't currently fetch — add `useQuery(["servers"],
  () => api<Server[]>("/api/servers"))` if not already present (check the top of
  the file first; `serverIdByName` at `:399` implies a servers fetch already
  exists — reuse it) and, per server in `used_by`, tag "direct" if `guid` is in
  that server's own `mods` array, else "indirect" (covered via another pick's
  closure — use `buildFlat` rooted at that server's explicit guids and check
  membership).

**Done when.** `npm run build` passes.

**Watch out.** `buildReverse`'s two overloads (S2) need a `mode` argument at the
call site — TypeScript will not infer which overload from a runtime toggle
variable typed `"nested" | "flat"` cleanly; either branch the call
(`if (mode === "nested") buildReverse(guid, graph, "nested") else
buildReverse(guid, graph, "flat")`) or accept a union return type and narrow at
render time. Don't fight the type system with `as any`.

---

## Phase 4 — Cleanup

### S10 — Delete `modCoverage.ts`, final sweep

**Deliver.** Once S7 and S8 (the only two importers of
`frontend/src/lib/modCoverage.ts`) are both merged and neither file imports it
any more (`grep -rn "modCoverage" frontend/src` returns nothing), delete
`frontend/src/lib/modCoverage.ts`.

**Done when.** `grep -rn "modCoverage" frontend/src` is empty, and `cd frontend
&& npm run build` passes clean.

**Watch out.** This is a one-file deletion — don't bundle any other change into
it. If `npm run build` fails after the delete, something still imports the old
module; find and fix that import rather than restoring the file.
