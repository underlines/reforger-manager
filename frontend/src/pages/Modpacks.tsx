import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Empty } from "../components/Empty";
import { PageHeading } from "../components/PageHeading";
import { ModLibraryPicker } from "../components/mods/ModLibraryPicker";
import { ModTree } from "../components/mods/ModTree";
import { SortableModList } from "../components/mods/SortableModList";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Dialog, Input } from "../components/ui";
import { api, ApiError, apiVoid, type Modpack, type Server } from "../lib/api";
import { buildNested, coverageFromGraph, useModGraph, type ModGraph } from "../lib/modGraph";

/* ------------------------------------------------------------------ *
 * A modpack item is only a GUID + a load order. `SortableModList` is
 * the generic drag list from S5 — it needs `{ key, guid, name }` and
 * nothing more, so `EditorItem` satisfies it directly (no `enabled`,
 * no pins). We pass `items` / `onReorder` for the top level only, a
 * small `renderActions` (disclosure + remove) and a `renderExpanded`
 * dependency subtree; no new prop on the component.
 * ------------------------------------------------------------------ */
type EditorItem = { key: string; guid: string; name: string | null; mod_guid: string };

/** Stable empty set for rows that cover nothing (avoids a new Set() each render). */
const EMPTY_GUIDS: ReadonlySet<string> = new Set<string>();

/** Stable empty graph while the shared graph query is still loading. */
const EMPTY_GRAPH: ModGraph = { nodes: [], edges: [] };

type ExportShape = {
  name: string;
  description: string | null;
  items: Array<{ mod_guid: string; load_order: number }>;
};

type ApplyResult = {
  applied?: number;
  dropped?: number;
  dropped_pins?: Array<{ mod_guid?: string; mod_name?: string | null }>;
  pins_dropped?: Array<{ mod_guid?: string; mod_name?: string | null }>;
};

const errText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

const safeFilename = (name: string) =>
  `${name.trim().replace(/[^\w.-]+/g, "_") || "modpack"}.modpack.json`;

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function itemsFromPack(pack: Modpack): EditorItem[] {
  return [...pack.items]
    .sort((a, b) => a.load_order - b.load_order || a.mod_guid.localeCompare(b.mod_guid))
    .map((item) => ({
      key: item.mod_guid,
      guid: item.mod_guid,
      name: item.mod_name,
      mod_guid: item.mod_guid,
    }));
}

function droppedPinNames(result: ApplyResult): string[] {
  const list = result.dropped_pins ?? result.pins_dropped ?? [];
  return list.map((pin) => pin.mod_name ?? pin.mod_guid ?? "unknown mod");
}

export function ModpacksPage() {
  const queryClient = useQueryClient();
  const packs = useQuery({ queryKey: ["modpacks"], queryFn: () => api<Modpack[]>("/api/modpacks") });

  const [notice, setNotice] = useState<string | null>(null);

  /* ------------------------------ editor ------------------------------ */

  const [editorOpen, setEditorOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [items, setItems] = useState<EditorItem[]>([]);
  const [formError, setFormError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"flat" | "nested">("flat");

  const itemGuids = useMemo(() => new Set(items.map((item) => item.mod_guid)), [items]);

  const resetEditor = () => {
    setName("");
    setDescription("");
    setItems([]);
    setFormError(null);
  };
  const openCreate = () => {
    resetEditor();
    setEditingId(null);
    setEditorOpen(true);
  };
  const openEdit = (pack: Modpack) => {
    setEditingId(pack.id);
    setName(pack.name);
    setDescription(pack.description ?? "");
    setItems(itemsFromPack(pack));
    setFormError(null);
    setEditorOpen(true);
  };
  const closeEditor = () => {
    setEditorOpen(false);
    setEditingId(null);
  };

  const makeItem = (guid: string, name: string | null): EditorItem => ({
    key: guid,
    guid,
    name,
    mod_guid: guid,
  });

  // Add only the explicit pick, synchronously — dependencies render as derived
  // coverage under each parent's disclosure below. Nothing the user did not
  // pick is stored.
  const addItem = (mod: { guid: string; name: string | null }) => {
    setItems((current) => {
      const present = new Set(current.map((item) => item.mod_guid));
      return present.has(mod.guid)
        ? current
        : [...current, makeItem(mod.guid, mod.name)];
    });
  };
  const removeItem = (guid: string) =>
    setItems((current) => current.filter((item) => item.mod_guid !== guid));

  /* -------------------- dependency-aware dedup (view only) -------------------- *
   * A pack stores only explicit picks. When one pick already pulls another in
   * through its dependency closure, the covered pick is hoisted out of the
   * top-level list and shown under its covering parent(s). Nothing is deleted —
   * a covered row keeps its slot in `items`; it returns to the top level once
   * no covering parent remains.
   * ------------------------------------------------------------------------- */

  const graphQuery = useModGraph();
  const graph = graphQuery.data ?? EMPTY_GRAPH;
  const { coverageByParent, covered } = coverageFromGraph(
    items.map((item) => item.mod_guid),
    graphQuery.data,
  );

  // Only uncovered picks reach the sortable list; drag / save still act on the
  // full `items` array (see `reorderVisible`).
  const topLevel = useMemo(
    () => items.filter((item) => !covered.has(item.mod_guid)),
    [items, covered],
  );

  // A drag reorders `topLevel`; splice that order back into `items`, leaving
  // covered rows pinned to the slots they already hold — `save` maps index →
  // load_order, so this keeps a covered pick stored next to its parent.
  const reorderVisible = (nextVisible: EditorItem[]) => {
    setItems((current) => {
      const queue = [...nextVisible];
      return current.map((row) =>
        covered.has(row.mod_guid) ? row : queue.shift() ?? row,
      );
    });
  };

  // Per-row "show dependencies" disclosure for the top-level list.
  const [expandedRows, setExpandedRows] = useState<Set<string>>(() => new Set());
  const toggleExpanded = (guid: string) =>
    setExpandedRows((current) => {
      const next = new Set(current);
      if (next.has(guid)) next.delete(guid);
      else next.add(guid);
      return next;
    });

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        name: name.trim(),
        description: description.trim() || null,
        items: items.map((item, index) => ({ mod_guid: item.mod_guid, load_order: index })),
      };
      return editingId == null
        ? api<Modpack>("/api/modpacks", { method: "POST", body: JSON.stringify(payload) })
        : api<Modpack>(`/api/modpacks/${editingId}`, {
            method: "PATCH",
            body: JSON.stringify(payload),
          });
    },
    onSuccess: async (pack) => {
      await queryClient.invalidateQueries({ queryKey: ["modpacks"] });
      setNotice(`Pack "${pack.name}" saved (${pack.items.length} mod${pack.items.length === 1 ? "" : "s"}).`);
      closeEditor();
    },
    onError: (error) => setFormError(errText(error, "Save failed")),
  });

  /* ------------------------------ apply ------------------------------ */

  const [applyTarget, setApplyTarget] = useState<Modpack | null>(null);
  const [applyServerId, setApplyServerId] = useState("");
  const [applyMode, setApplyMode] = useState<"replace" | "append">("replace");
  const [applyResult, setApplyResult] = useState<ApplyResult | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);

  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: () => api<Server[]>("/api/servers"),
    enabled: applyTarget !== null,
  });

  const openApply = (pack: Modpack) => {
    setApplyTarget(pack);
    setApplyServerId("");
    setApplyMode("replace");
    setApplyResult(null);
    setApplyError(null);
  };

  const apply = useMutation({
    mutationFn: () =>
      api<ApplyResult>(`/api/modpacks/${applyTarget?.id}/apply/${applyServerId}`, {
        method: "POST",
        body: JSON.stringify({ mode: applyMode }),
      }),
    onSuccess: (result) => {
      setApplyResult(result);
      setApplyError(null);
      void queryClient.invalidateQueries({ queryKey: ["servers"] });
      void queryClient.invalidateQueries({ queryKey: ["server"] });
    },
    onError: (error) => {
      setApplyResult(null);
      setApplyError(
        error instanceof ApiError && error.status === 409
          ? error.message || "That server is running — stop it before applying a pack."
          : errText(error, "Apply failed"),
      );
    },
  });

  /* ------------------------------ delete ------------------------------ */

  const [deleteTarget, setDeleteTarget] = useState<Modpack | null>(null);
  const remove = useMutation({
    mutationFn: (id: number) => apiVoid(`/api/modpacks/${id}`, { method: "DELETE" }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["modpacks"] });
      setNotice(`Pack "${deleteTarget?.name ?? ""}" deleted.`);
      setDeleteTarget(null);
    },
    onError: (error) => setNotice(errText(error, "Delete failed")),
  });

  /* ------------------------------ export ------------------------------ */

  const exportPack = async (pack: Modpack) => {
    setNotice(null);
    try {
      const data = await api<ExportShape>(`/api/modpacks/${pack.id}/export`);
      downloadJson(safeFilename(pack.name), data);
      setNotice(`Exported "${pack.name}".`);
    } catch (error) {
      setNotice(errText(error, "Export failed"));
    }
  };

  /* ------------------------------ import ------------------------------ */

  const [importOpen, setImportOpen] = useState(false);
  const [importConflict, setImportConflict] = useState<"rename" | "replace" | "error">("rename");
  const [importError, setImportError] = useState<string | null>(null);
  const [importResult, setImportResult] = useState<Modpack | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const openImport = () => {
    setImportConflict("rename");
    setImportError(null);
    setImportResult(null);
    setImportOpen(true);
  };

  const doImport = useMutation({
    mutationFn: (body: unknown) =>
      api<Modpack>(`/api/modpacks/import?on_conflict=${importConflict}`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: async (pack) => {
      setImportError(null);
      setImportResult(pack);
      await queryClient.invalidateQueries({ queryKey: ["modpacks"] });
    },
    onError: (error) =>
      setImportError(
        error instanceof ApiError && error.status === 409
          ? error.message || "A pack with that name already exists."
          : errText(error, "Import failed"),
      ),
  });

  const onImportFile = async (file: File) => {
    setImportError(null);
    setImportResult(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      setImportError("That file is not valid JSON.");
      return;
    }
    if (typeof parsed !== "object" || parsed === null || !("items" in parsed)) {
      setImportError('Not a modpack export — expected an object with "name" and "items".');
      return;
    }
    doImport.mutate(parsed);
  };

  /* ------------------------------ render ------------------------------ */

  return (
    <>
      <PageHeading
        title="Modpacks"
        detail="Reusable, ordered mod loadouts for repeatable server deployment."
        actions={
          <>
            <Button variant="outline" onClick={openImport}>
              Import
            </Button>
            <Button onClick={openCreate}>New pack</Button>
          </>
        }
      />

      {notice && (
        <p className="mb-4 text-xs text-stone-300" role="status">
          {notice}
        </p>
      )}

      {editorOpen && (
        <Card className="mb-4">
          <CardHeader>
            <CardTitle>{editingId == null ? "New modpack" : `Edit ${name || "modpack"}`}</CardTitle>
          </CardHeader>
          <CardContent>
            <form
              className="grid gap-5"
              onSubmit={(event: FormEvent) => {
                event.preventDefault();
                if (name.trim()) save.mutate();
              }}
            >
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block space-y-1 text-xs text-stone-300">
                  <span>Name</span>
                  <Input value={name} onChange={(event) => setName(event.target.value)} required autoFocus />
                </label>
                <label className="block space-y-1 text-xs text-stone-300">
                  <span>Description (optional)</span>
                  <Input
                    value={description}
                    onChange={(event) => setDescription(event.target.value)}
                    placeholder="Operational notes"
                  />
                </label>
              </div>

              <section className="grid gap-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="metric-label">Pack contents ({items.length})</p>
                  <div className="flex items-center gap-1">
                    <Button
                      size="sm"
                      type="button"
                      variant={viewMode === "flat" ? "outline" : "ghost"}
                      aria-pressed={viewMode === "flat"}
                      onClick={() => setViewMode("flat")}
                    >
                      Flat
                    </Button>
                    <Button
                      size="sm"
                      type="button"
                      variant={viewMode === "nested" ? "outline" : "ghost"}
                      aria-pressed={viewMode === "nested"}
                      onClick={() => setViewMode("nested")}
                    >
                      Nested
                    </Button>
                  </div>
                </div>
                {viewMode === "flat" ? (
                  <>
                    {items.length ? (
                      <SortableModList
                        items={topLevel}
                        onReorder={reorderVisible}
                        renderActions={(item) => {
                          const open = expandedRows.has(item.mod_guid);
                          return (
                            <>
                              <Button
                                size="sm"
                                variant="ghost"
                                type="button"
                                aria-expanded={open}
                                aria-label={open ? "Hide dependencies" : "Show dependencies"}
                                onClick={() => toggleExpanded(item.mod_guid)}
                              >
                                {open ? "▾" : "▸"}
                              </Button>
                              <Button
                                size="sm"
                                variant="ghost"
                                type="button"
                                onClick={() => removeItem(item.mod_guid)}
                              >
                                Remove
                              </Button>
                            </>
                          );
                        }}
                        renderExpanded={(item) =>
                          expandedRows.has(item.mod_guid) ? (
                            <PackItemExpanded
                              parent={item}
                              items={items}
                              graph={graph}
                              coverageGuids={coverageByParent.get(item.mod_guid) ?? EMPTY_GUIDS}
                              onRemove={removeItem}
                            />
                          ) : null
                        }
                      />
                    ) : (
                      <Empty label="No mods in this pack yet — add some from the library below." />
                    )}
                    <p className="text-[10px] text-stone-500">
                      Drag the handle to reorder — load order is significant.
                    </p>
                  </>
                ) : (
                  <>
                    <ModTree
                      graph={graph}
                      mode="nested"
                      nested={buildNested(
                        items.map((item) => item.mod_guid),
                        graph,
                      )}
                      linkTo={(guid) => `/mods/${guid}`}
                    />
                    <p className="text-[10px] text-stone-500">
                      Switch to Flat to reorder or remove items.
                    </p>
                  </>
                )}
              </section>

              <section className="grid gap-3">
                <p className="metric-label">Add mods from the library</p>
                <ModLibraryPicker
                  disabledGuids={new Set([...itemGuids, ...covered])}
                  onAdd={(mod) => addItem(mod)}
                  showDepsPreview={false}
                  pageSize={Infinity}
                />
              </section>

              {formError && <p className="error">{formError}</p>}

              <div className="flex flex-wrap items-center gap-2">
                <Button type="submit" disabled={save.isPending || !name.trim()}>
                  {save.isPending
                    ? "Saving..."
                    : editingId == null
                      ? "Create pack"
                      : "Save changes"}
                </Button>
                <Button type="button" variant="ghost" onClick={closeEditor} disabled={save.isPending}>
                  Cancel
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Packs {packs.data ? `(${packs.data.length})` : ""}</CardTitle>
        </CardHeader>
        <CardContent>
          {packs.isLoading ? (
            <p className="text-sm text-stone-400">Loading modpacks...</p>
          ) : packs.isError ? (
            <div className="space-y-3">
              <p className="error">Could not load modpacks: {errText(packs.error, "Request failed")}</p>
              <Button size="sm" variant="outline" onClick={() => void packs.refetch()}>
                Retry
              </Button>
            </div>
          ) : packs.data?.length ? (
            <ul className="divide-y divide-stone-800">
              {packs.data.map((pack) => (
                <li key={pack.id} className="grid gap-3 py-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-display text-lg uppercase tracking-wide text-stone-100">
                        {pack.name}
                      </span>
                      <Badge tone="neutral">
                        {pack.items.length} mod{pack.items.length === 1 ? "" : "s"}
                      </Badge>
                    </div>
                    {pack.description && (
                      <p className="text-xs leading-5 text-stone-400">{pack.description}</p>
                    )}
                  </div>
                  <div className="flex flex-wrap content-start gap-2 lg:justify-end">
                    <Button size="sm" variant="ghost" onClick={() => openEdit(pack)}>
                      Edit
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => openApply(pack)}>
                      Apply to...
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => void exportPack(pack)}>
                      Export
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setDeleteTarget(pack)}>
                      Delete
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty label="No modpacks yet. Create one, or import a pack JSON." />
          )}
        </CardContent>
      </Card>

      {/* ------------------------------ apply dialog ------------------------------ */}
      <Dialog
        open={applyTarget !== null}
        title={`Apply ${applyTarget?.name ?? "pack"}`}
        onClose={() => !apply.isPending && setApplyTarget(null)}
      >
        <div className="space-y-4">
          <label className="block space-y-1 text-xs text-stone-300">
            <span>Target server definition</span>
            <select
              className="h-10 w-full border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100"
              value={applyServerId}
              onChange={(event) => setApplyServerId(event.target.value)}
            >
              <option value="">Select a server...</option>
              {(servers.data ?? []).map((server) => (
                <option key={server.id} value={String(server.id)}>
                  {server.name}
                  {server.is_running ? " (running)" : ""}
                </option>
              ))}
            </select>
          </label>
          {servers.isError && (
            <p className="error">{errText(servers.error, "Could not load servers.")}</p>
          )}

          <fieldset className="space-y-2">
            <legend className="text-xs text-stone-300">Mode</legend>
            <label className="flex items-center gap-2 text-xs text-stone-300">
              <input
                type="radio"
                name="apply-mode"
                checked={applyMode === "replace"}
                onChange={() => setApplyMode("replace")}
              />
              Replace — clear the server's mod set, then write the pack
            </label>
            <label className="flex items-center gap-2 text-xs text-stone-300">
              <input
                type="radio"
                name="apply-mode"
                checked={applyMode === "append"}
                onChange={() => setApplyMode("append")}
              />
              Append — add the pack's mods, keeping what is already there
            </label>
          </fieldset>

          {applyError && <p className="error">{applyError}</p>}
          {applyResult && (
            <div className="space-y-1 text-xs text-emerald-400">
              <p>Applied {applyResult.applied ?? 0} mod{(applyResult.applied ?? 0) === 1 ? "" : "s"}.</p>
              {droppedPinNames(applyResult).length > 0 && (
                <p className="text-amber-300">
                  These pins were dropped because their mod left the set:{" "}
                  {droppedPinNames(applyResult).join(", ")}
                </p>
              )}
            </div>
          )}

          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setApplyTarget(null)}
              disabled={apply.isPending}
            >
              {applyResult ? "Done" : "Cancel"}
            </Button>
            <Button
              type="button"
              onClick={() => apply.mutate()}
              disabled={apply.isPending || !applyServerId}
            >
              {apply.isPending ? "Applying..." : "Apply pack"}
            </Button>
          </div>
        </div>
      </Dialog>

      {/* ------------------------------ delete dialog ------------------------------ */}
      <Dialog
        open={deleteTarget !== null}
        title={`Delete ${deleteTarget?.name ?? "pack"}`}
        onClose={() => !remove.isPending && setDeleteTarget(null)}
      >
        <div className="space-y-4">
          <p className="text-xs text-stone-300">
            This permanently removes the pack. Server definitions it was applied to are not
            affected. This cannot be undone.
          </p>
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteTarget(null)}
              disabled={remove.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => deleteTarget && remove.mutate(deleteTarget.id)}
              disabled={remove.isPending}
            >
              {remove.isPending ? "Deleting..." : "Delete"}
            </Button>
          </div>
        </div>
      </Dialog>

      {/* ------------------------------ import dialog ------------------------------ */}
      <Dialog
        open={importOpen}
        title="Import modpack"
        onClose={() => !doImport.isPending && setImportOpen(false)}
      >
        <div className="space-y-4">
          <p className="text-xs leading-5 text-stone-400">
            Select a <code>.modpack.json</code> file exported from this or another instance.
          </p>
          <label className="block space-y-1 text-xs text-stone-300">
            <span>On name conflict</span>
            <select
              className="h-10 w-full border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100"
              value={importConflict}
              onChange={(event) =>
                setImportConflict(event.target.value as "rename" | "replace" | "error")
              }
            >
              <option value="rename">Rename — import as a new pack with a suffixed name</option>
              <option value="replace">Replace — overwrite the existing pack of that name</option>
              <option value="error">Error — refuse if the name is taken</option>
            </select>
          </label>

          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void onImportFile(file);
            }}
          />
          <Button
            type="button"
            variant="outline"
            onClick={() => fileRef.current?.click()}
            disabled={doImport.isPending}
          >
            {doImport.isPending ? "Importing..." : "Choose file..."}
          </Button>

          {importError && <p className="error">{importError}</p>}
          {importResult && (
            <p className="text-xs text-emerald-400">
              Imported "{importResult.name}" with {importResult.items.length} mod
              {importResult.items.length === 1 ? "" : "s"}.
            </p>
          )}

          <div className="flex justify-end">
            <Button type="button" variant="ghost" onClick={() => setImportOpen(false)} disabled={doImport.isPending}>
              {importResult ? "Done" : "Cancel"}
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}

/* Expanded subtree under one pack pick: the OTHER explicit picks its dependency
 * closure covers (rendered with a Remove — the row is stored in the pack) plus
 * its dependency subtree (read-only links, sourced from the shared mod graph).
 * Modeled on the Mods tab's AssignedModExpanded, minus enable/pin controls —
 * packs have neither. */
function PackItemExpanded({
  parent,
  items,
  graph,
  coverageGuids,
  onRemove,
}: {
  parent: EditorItem;
  items: EditorItem[];
  graph: ModGraph;
  coverageGuids: ReadonlySet<string>;
  onRemove: (mod_guid: string) => void;
}) {
  // The parent guid itself is dropped so it isn't re-rendered inside its own
  // expanded section — only its dependency subtree.
  const dependencyNodes = buildNested([parent.mod_guid], graph).flatMap((node) => node.children);
  const picks = items.filter((row) => coverageGuids.has(row.mod_guid));

  return (
    <div className="mt-2 w-full space-y-1.5 border-l-2 border-stone-800 pl-3 text-[11px] text-stone-400">
      {picks.map((row) => (
        <div key={row.mod_guid} className="flex flex-wrap items-center gap-2">
          <Link
            to={`/mods/${row.mod_guid}`}
            className="font-display uppercase tracking-wide text-stone-200 hover:text-amber-400"
            onClick={(event) => event.stopPropagation()}
          >
            {row.name ?? row.mod_guid}
          </Link>
          <Badge tone="neutral">pick</Badge>
          <Button
            size="sm"
            variant="ghost"
            type="button"
            onClick={() => onRemove(row.mod_guid)}
          >
            Remove
          </Button>
        </div>
      ))}

      {dependencyNodes.length ? (
        <ModTree
          graph={graph}
          mode="nested"
          nested={dependencyNodes}
          linkTo={(guid) => `/mods/${guid}`}
        />
      ) : picks.length ? null : (
        <p>No dependencies.</p>
      )}
    </div>
  );
}
