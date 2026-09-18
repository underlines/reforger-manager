import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Empty } from "../Empty";
import { Button, Input } from "../ui";
import { api } from "../../lib/api";

/* ------------------------------------------------------------------ *
 * Shared "add a mod from the library" picker. Consolidates the two
 * near-duplicate inline pickers (ModsPanel's AddModRow section and the
 * Modpacks editor's plain list). Fully self-contained: the parent only
 * supplies `disabledGuids` (already-covered guids — rows grey out,
 * they are NOT hidden) and `onAdd`. The library query key/fn are kept
 * identical to the two call sites so the react-query cache is shared.
 *
 * showDepsPreview=true renders the ModsPanel-style picker: per-row
 * expandable dependency preview, the slice(0, 60) cap and 10-per-page
 * pagination (override with `pageSize`). showDepsPreview=false renders
 * the Modpacks-style plain list: search-filtered, uncapped, unpaged.
 * ------------------------------------------------------------------ */
export type ModRecord = {
  guid: string;
  name: string | null;
  size: number | null;
  is_local: boolean;
};

type ModLibraryPickerProps = {
  disabledGuids: ReadonlySet<string>;
  onAdd: (mod: ModRecord) => void;
  showDepsPreview?: boolean;
  pageSize?: number;
};

type DepsResponse = {
  dependency_tree: { nodes: { guid: string; name: string | null; depth: number }[] } | null;
};

const formatSize = (bytes: number | null) => {
  if (bytes === null || bytes === undefined) return "size unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
};

const errText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

export function ModLibraryPicker({
  disabledGuids,
  onAdd,
  showDepsPreview = true,
  pageSize = 10,
}: ModLibraryPickerProps) {
  const [modSearch, setModSearch] = useState("");
  const [showNonLocal, setShowNonLocal] = useState(false);
  const library = useQuery({
    queryKey: ["mods", "library", showNonLocal],
    queryFn: () => api<ModRecord[]>(`/api/mods${showNonLocal ? "" : "?local=true"}`),
  });

  // Every candidate renders — guids already covered by the parent's set stay
  // in the list and are greyed out via the row's disabled Add button.
  const candidates = useMemo(() => {
    const query = modSearch.trim().toLowerCase();
    const filtered = (library.data ?? []).filter(
      (mod) =>
        !query ||
        (mod.name ?? "").toLowerCase().includes(query) ||
        mod.guid.toLowerCase().includes(query),
    );
    // Deliberate guard carried over from the ModsPanel picker: bound the rows
    // that can each resolve a dependency tree on demand.
    return showDepsPreview ? filtered.slice(0, 60) : filtered;
  }, [library.data, modSearch, showDepsPreview]);

  // Pagination: 10 rows per page by default, back to page 1 whenever the
  // filter or the non-local toggle changes. pageIndex clamps so a shrinking
  // candidate list can't leave an empty slice on screen.
  const paged = pageSize !== Infinity;
  const [page, setPage] = useState(0);
  useEffect(() => setPage(0), [modSearch, showNonLocal]);
  const pageCount = Math.max(1, Math.ceil(candidates.length / pageSize));
  const pageIndex = Math.min(page, pageCount - 1);
  const pageItems = paged
    ? candidates.slice(pageIndex * pageSize, pageIndex * pageSize + pageSize)
    : candidates;

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <Input
          className="h-9 max-w-xs"
          value={modSearch}
          onChange={(event) => setModSearch(event.target.value)}
          placeholder="Filter library by name or GUID..."
        />
        <label className="flex items-center gap-2 text-xs text-stone-300">
          <input
            type="checkbox"
            checked={showNonLocal}
            onChange={(event) => setShowNonLocal(event.target.checked)}
          />
          Include not-local mods
        </label>
      </div>

      {library.isLoading ? (
        <p className="text-xs text-stone-400">Loading library...</p>
      ) : library.isError ? (
        <p className="error">{errText(library.error, "Library could not be loaded.")}</p>
      ) : candidates.length ? (
        <>
          <ul className="divide-y divide-stone-800 border border-stone-800">
            {pageItems.map((mod) => (
              <PickerRow
                key={mod.guid}
                mod={mod}
                disabledGuids={disabledGuids}
                showDeps={showDepsPreview}
                onAdd={onAdd}
              />
            ))}
          </ul>
          {paged && (
            <div className="flex items-center justify-between gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={pageIndex === 0}
                onClick={() => setPage(pageIndex - 1)}
              >
                Prev
              </Button>
              <span className="text-xs text-stone-400">
                {pageIndex * pageSize + 1}-
                {Math.min((pageIndex + 1) * pageSize, candidates.length)} of {candidates.length}
              </span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={pageIndex + 1 >= pageCount}
                onClick={() => setPage(pageIndex + 1)}
              >
                Next
              </Button>
            </div>
          )}
        </>
      ) : (
        <Empty label="No library mods match — every candidate is already assigned or filtered out." />
      )}
    </div>
  );
}

function PickerRow({
  mod,
  disabledGuids,
  showDeps,
  onAdd,
}: {
  mod: ModRecord;
  disabledGuids: ReadonlySet<string>;
  showDeps: boolean;
  onAdd: (mod: ModRecord) => void;
}) {
  const [open, setOpen] = useState(false);
  // Shares the ["mod", guid, "deps"] cache with useModCoverage and the other
  // consumers; only fetches once the row's disclosure is opened.
  const detail = useQuery({
    queryKey: ["mod", mod.guid, "deps"],
    queryFn: () => api<DepsResponse>(`/api/mods/${mod.guid}`),
    enabled: open,
  });

  const additions = useMemo(() => {
    const nodes = detail.data?.dependency_tree?.nodes ?? [];
    return nodes.filter(
      (node) => node.depth > 0 && node.guid !== mod.guid && !disabledGuids.has(node.guid),
    );
  }, [detail.data, mod.guid, disabledGuids]);

  const covered = disabledGuids.has(mod.guid);

  return (
    <li className={showDeps ? "grid gap-2 px-3 py-2" : "flex flex-wrap items-center gap-3 px-3 py-2"}>
      <div className="flex flex-wrap items-center gap-3">
        <span className="min-w-0 flex-1">
          <span className="block truncate font-display text-sm uppercase tracking-wide text-stone-100">
            {mod.name ?? mod.guid}
          </span>
          <span className="flex flex-wrap gap-x-3 text-[10px] text-stone-500">
            <span className="font-mono">{mod.guid}</span>
            {showDeps && <span>{formatSize(mod.size)}</span>}
            {!mod.is_local && <span className="text-amber-400">not local</span>}
          </span>
        </span>
        {showDeps && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
          >
            {open ? "Hide deps" : "Deps"}
          </Button>
        )}
        <Button
          type="button"
          size="sm"
          disabled={covered}
          title={covered ? "Already in the set" : undefined}
          onClick={() => onAdd(mod)}
        >
          Add
        </Button>
      </div>
      {showDeps && open && (
        <div className="text-[11px] text-stone-400">
          {detail.isLoading ? (
            "Resolving dependencies..."
          ) : detail.isError ? (
            <span className="error">Could not resolve dependencies.</span>
          ) : additions.length ? (
            <span>
              Brings {additions.length} dependency{additions.length === 1 ? "" : "ies"} not yet in
              the set: {additions.map((node) => node.name ?? node.guid).join(", ")}
            </span>
          ) : (
            "No new dependencies — everything it needs is already assigned."
          )}
        </div>
      )}
    </li>
  );
}
