import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Empty } from "../Empty";
import { SortableModList, type SortableRow } from "../mods/SortableModList";
import { Badge, Button, Dialog, Input } from "../ui";
import {
  api,
  ApiError,
  type DetailServer,
  type Mod,
  type Modpack,
  type Preflight,
  type Server,
  type ServerMod,
} from "../../lib/api";

/* ------------------------------------------------------------------ *
 * Local working copy of a server's mod set. Every edit on this tab
 * mutates `working`; nothing is persisted until "Save mod set".
 * The pin fields are carried verbatim so they round-trip through the
 * PATCH transport (Watch out (a) — belt and braces with the S2 fix).
 * ------------------------------------------------------------------ */
type WorkingMod = SortableRow & {
  mod_guid: string;
  mod_name: string | null;
  enabled: boolean;
  pinned_version: string | null;
  pinned_at_build: string | null;
  pinned_reason: string | null;
  pinned_at: string | null;
};

type DepNode = { guid: string; name: string | null; via: string; state: string; depth: number };
type ModDeps = { dependency_tree: { nodes: DepNode[] } | null };

/** Stable empty set for rows that cover nothing (avoids a new Set() each render). */
const EMPTY_GUIDS: ReadonlySet<string> = new Set<string>();

/* Response of POST /api/modpacks/{pack_id}/apply/{server_id} — a ModpackApplyOut. */
export type ApplyModpackResult = {
  applied: number;
  mode: "replace" | "append";
  dropped_pins: Array<{ mod_guid: string; mod_name: string | null }>;
};

/* Response of POST /api/modpacks/from-server/{id} — a ModpackOut, possibly with
 * a note that the source server's pins were not carried into the pack. Shape may
 * vary slightly, so read it defensively. */
type FromServerResult = {
  id: number;
  name: string;
  items?: unknown[];
  note?: string | null;
  pins_note?: string | null;
  dropped_pins?: Array<{ mod_name?: string | null; mod_guid?: string }>;
};

function packPinsNote(result: FromServerResult): string | null {
  if (result.note) return result.note;
  if (result.pins_note) return result.pins_note;
  const dropped = result.dropped_pins ?? [];
  if (dropped.length) {
    const names = dropped.map((pin) => pin.mod_name ?? pin.mod_guid ?? "a mod").join(", ");
    return `Version pins were not carried into the pack: ${names}.`;
  }
  return null;
}

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

function fromServerMods(mods: ServerMod[]): WorkingMod[] {
  return [...mods]
    .sort((a, b) => a.load_order - b.load_order || a.mod_guid.localeCompare(b.mod_guid))
    .map((mod) => ({
      key: mod.mod_guid,
      guid: mod.mod_guid,
      name: mod.mod_name,
      mod_guid: mod.mod_guid,
      mod_name: mod.mod_name,
      enabled: mod.enabled,
      pinned_version: mod.pinned_version ?? null,
      pinned_at_build: mod.pinned_at_build ?? null,
      pinned_reason: mod.pinned_reason ?? null,
      pinned_at: mod.pinned_at ?? null,
    }));
}

/** Order-sensitive comparison of the parts a save actually persists. */
function modsEqual(a: WorkingMod[], b: WorkingMod[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((row, index) => {
    const other = b[index];
    return (
      row.mod_guid === other.mod_guid &&
      row.enabled === other.enabled &&
      row.pinned_version === other.pinned_version &&
      row.pinned_at_build === other.pinned_at_build &&
      row.pinned_reason === other.pinned_reason
    );
  });
}

export function ModsPanel({
  id,
  server,
  action,
  run,
}: {
  id: string;
  server: DetailServer;
  action: string | null;
  run: (name: string, request: () => Promise<unknown>) => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  // Pre-flight runs on request only — never on mount / focus / reconnect. The
  // toolbar "Refresh pre-flight" button and the post-save / post-pin /
  // post-modpack-apply hooks below call `refetch()` explicitly.
  const preflight = useQuery({
    queryKey: ["server-preflight", id],
    queryFn: () => api<Preflight>(`/api/servers/${id}/preflight`),
    enabled: false,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const tone =
    preflight.data?.verdict === "green"
      ? "good"
      : preflight.data?.verdict === "blocked"
        ? "bad"
        : "warn";

  /* --------------------------- working copy --------------------------- */

  const serverModsSig = useMemo(
    () =>
      JSON.stringify(
        [...server.mods]
          .sort((a, b) => a.mod_guid.localeCompare(b.mod_guid))
          .map((mod) => [
            mod.mod_guid,
            mod.mod_name,
            mod.load_order,
            mod.enabled,
            mod.pinned_version,
            mod.pinned_at_build,
            mod.pinned_reason,
          ]),
      ),
    [server.mods],
  );

  const [working, setWorking] = useState<WorkingMod[]>(() => fromServerMods(server.mods));
  const [baseline, setBaseline] = useState<WorkingMod[]>(() => fromServerMods(server.mods));

  const dirty = useMemo(() => !modsEqual(working, baseline), [working, baseline]);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  // Resync from the server whenever its persisted set changes — but never
  // clobber an in-progress edit (a background refetch, a start/stop, or the
  // inline pin call below all invalidate ["server", id]).
  useEffect(() => {
    if (dirtyRef.current) return;
    const next = fromServerMods(server.mods);
    setWorking(next);
    setBaseline(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverModsSig]);

  const workingGuids = useMemo(() => new Set(working.map((mod) => mod.mod_guid)), [working]);
  const persistedGuids = useMemo(
    () => new Set(server.mods.map((mod) => mod.mod_guid)),
    [server.mods],
  );

  const setEnabled = (guid: string, enabled: boolean) =>
    setWorking((current) =>
      current.map((mod) => (mod.mod_guid === guid ? { ...mod, enabled } : mod)),
    );
  const removeMod = (guid: string) =>
    setWorking((current) => current.filter((mod) => mod.mod_guid !== guid));

  const makeWorkingMod = (guid: string, name: string | null): WorkingMod => ({
    key: guid,
    guid,
    name,
    mod_guid: guid,
    mod_name: name,
    enabled: true,
    pinned_version: null,
    pinned_at_build: null,
    pinned_reason: null,
    pinned_at: null,
  });

  // Dependencies are resolved into the generated config at start time
  // (config_gen closure) — the assigned list holds only explicit picks.
  const addMod = (mod: Mod) => {
    setWorking((current) => {
      const present = new Set(current.map((row) => row.mod_guid));
      return present.has(mod.guid)
        ? current
        : [...current, makeWorkingMod(mod.guid, mod.name)];
    });
  };
  const discard = () => {
    const next = fromServerMods(server.mods);
    setWorking(next);
    setBaseline(next);
  };

  /* -------------------- dependency-aware dedup (view only) -------------------- *
   * The assigned list carries every explicit pick. But when one explicit pick
   * already pulls another in through its dependency closure, the covered pick is
   * hoisted out of the top-level list and shown under its covering parent(s).
   * Nothing is deleted — a covered row keeps its slot in `working`, its enable
   * toggle and its pin; only where it renders moves. It returns to the top level
   * once no covering parent remains.
   * ------------------------------------------------------------------------- */

  // One deps query per explicit pick, keyed exactly like AddModRow /
  // AssignedModExpanded so the three share a single cache entry.
  const depResults = useQueries({
    queries: working.map((mod) => ({
      queryKey: ["mod", mod.mod_guid, "deps"],
      queryFn: () => api<ModDeps>(`/api/mods/${mod.mod_guid}`),
      staleTime: 60_000,
    })),
  });

  // Signature that changes only when a deps query gains/loses data — keeps the
  // memo below from recomputing on every unrelated render.
  const depSig = depResults
    .map((result) => (result.data ? String(result.dataUpdatedAt) : result.status))
    .join("|");

  // guid -> set of directly-declared, resolved dependency guids (undefined while
  // that query is still loading → "covers nothing" for now).
  const depEdges = useMemo(() => {
    const map = new Map<string, Set<string> | undefined>();
    working.forEach((mod, index) => {
      const data = depResults[index]?.data;
      if (!data) {
        map.set(mod.mod_guid, undefined);
        return;
      }
      const nodes = data.dependency_tree?.nodes ?? [];
      map.set(
        mod.mod_guid,
        new Set(
          nodes
            .filter((node) => node.depth > 0 && node.state !== "unresolved")
            .map((node) => node.guid),
        ),
      );
    });
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [working, depSig]);

  // parent guid -> every OTHER explicit pick reachable through its closure.
  // Walked with a visited set so A→B→A metadata can't loop.
  const coverageByParent = useMemo(() => {
    const explicit = new Set(working.map((mod) => mod.mod_guid));
    const map = new Map<string, Set<string>>();
    for (const parent of working) {
      const reached = new Set<string>();
      const seen = new Set<string>([parent.mod_guid]);
      const stack = [...(depEdges.get(parent.mod_guid) ?? [])];
      while (stack.length) {
        const guid = stack.pop() as string;
        if (seen.has(guid)) continue;
        seen.add(guid);
        if (explicit.has(guid)) reached.add(guid);
        const next = depEdges.get(guid);
        if (next) for (const g of next) if (!seen.has(g)) stack.push(g);
      }
      reached.delete(parent.mod_guid);
      map.set(parent.mod_guid, reached);
    }
    return map;
  }, [working, depEdges]);

  const covered = useMemo(() => {
    const set = new Set<string>();
    for (const reached of coverageByParent.values())
      for (const guid of reached) set.add(guid);
    return set;
  }, [coverageByParent]);

  // Only uncovered rows reach the sortable list; DnD / save still act on the
  // full `working` array (see `reorderVisible`).
  const topLevel = useMemo(
    () => working.filter((mod) => !covered.has(mod.mod_guid)),
    [working, covered],
  );

  // A drag reorders `topLevel`; splice that order back into `working`, leaving
  // covered rows pinned to the slots they already hold.
  const reorderVisible = (nextVisible: WorkingMod[]) => {
    setWorking((current) => {
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

  /* ------------------------------ save ------------------------------ */

  const save = useMutation({
    mutationFn: () =>
      api<Server>(`/api/servers/${id}`, {
        method: "PATCH",
        body: JSON.stringify({
          mods: working.map((mod, index) => ({
            mod_guid: mod.mod_guid,
            mod_name: mod.mod_name,
            load_order: index,
            enabled: mod.enabled,
            // Round-trip the pin fields the row currently holds (Watch out (a)).
            pinned_version: mod.pinned_version,
            pinned_at_build: mod.pinned_at_build,
            pinned_reason: mod.pinned_reason,
          })),
        }),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["server", id] });
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
      await preflight.refetch();
    },
  });

  /* --------------------------- inline pin --------------------------- */

  // Pin/unpin hit the API immediately and return a fresh ServerOut. The mod
  // set is an unsaved working copy, so rather than replacing it (which would
  // drop a pending reorder / adds — Watch out (b)) we merge just the changed
  // mod's pin fields into both the working copy and its baseline.
  const mergePins = (guid: string, fresh: Server) => {
    const source = fresh.mods.find((mod) => mod.mod_guid === guid);
    const patch = {
      pinned_version: source?.pinned_version ?? null,
      pinned_at_build: source?.pinned_at_build ?? null,
      pinned_reason: source?.pinned_reason ?? null,
      pinned_at: source?.pinned_at ?? null,
    };
    const apply = (rows: WorkingMod[]) =>
      rows.map((mod) => (mod.mod_guid === guid ? { ...mod, ...patch } : mod));
    setWorking(apply);
    setBaseline(apply);
    void queryClient.invalidateQueries({ queryKey: ["server", id] });
    void preflight.refetch();
  };

  const [pinTarget, setPinTarget] = useState<WorkingMod | null>(null);
  const [pinVersion, setPinVersion] = useState("");
  const [pinReason, setPinReason] = useState("");

  const pinMutation = useMutation({
    mutationFn: ({ guid, version, reason }: { guid: string; version: string; reason: string }) =>
      api<Server>(`/api/servers/${id}/mods/${guid}/pin`, {
        method: "POST",
        body: JSON.stringify({ version, reason: reason || null }),
      }),
    onSuccess: (fresh, variables) => {
      mergePins(variables.guid, fresh);
      setPinTarget(null);
    },
  });
  const unpinMutation = useMutation({
    mutationFn: (guid: string) =>
      api<Server>(`/api/servers/${id}/mods/${guid}/pin`, { method: "DELETE" }),
    onSuccess: (fresh, guid) => mergePins(guid, fresh),
  });
  const pinBusy = pinMutation.isPending || unpinMutation.isPending;

  const openPinDialog = (mod: WorkingMod) => {
    setPinTarget(mod);
    setPinVersion(mod.pinned_version ?? "");
    setPinReason(mod.pinned_reason ?? "");
  };
  const submitPin = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!pinTarget || !pinVersion.trim()) return;
    pinMutation.mutate({
      guid: pinTarget.mod_guid,
      version: pinVersion.trim(),
      reason: pinReason.trim(),
    });
  };

  /* ---------------------------- add mods ---------------------------- */

  const [showNonLocal, setShowNonLocal] = useState(false);
  const [modSearch, setModSearch] = useState("");
  const library = useQuery({
    queryKey: ["mods", "library", showNonLocal],
    queryFn: () => api<Mod[]>(`/api/mods${showNonLocal ? "" : "?local=true"}`),
  });

  const candidates = useMemo(() => {
    const query = modSearch.trim().toLowerCase();
    return (library.data ?? [])
      .filter((mod) => !workingGuids.has(mod.guid))
      .filter(
        (mod) =>
          !query ||
          (mod.name ?? "").toLowerCase().includes(query) ||
          mod.guid.toLowerCase().includes(query),
      )
      .slice(0, 60);
  }, [library.data, workingGuids, modSearch]);

  /* ----------------------- save mod set as a pack ----------------------- */

  const [packOpen, setPackOpen] = useState(false);
  const [packName, setPackName] = useState("");
  const [packDesc, setPackDesc] = useState("");
  const [packNote, setPackNote] = useState<string | null>(null);

  const savePack = useMutation({
    mutationFn: () =>
      api<FromServerResult>(`/api/modpacks/from-server/${id}`, {
        method: "POST",
        body: JSON.stringify({
          name: packName.trim(),
          description: packDesc.trim() || null,
        }),
      }),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["modpacks"] });
      const note = packPinsNote(result);
      setPackOpen(false);
      setPackName("");
      setPackDesc("");
      if (note) {
        setPackNote(note);
      } else {
        navigate("/modpacks");
      }
    },
  });

  const submitPack = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (packName.trim()) savePack.mutate();
  };

  /* ----------------------- load a modpack into this server ----------------------- */

  const [loadOpen, setLoadOpen] = useState(false);
  const [loadPackId, setLoadPackId] = useState("");
  const [loadMode, setLoadMode] = useState<"replace" | "append">("replace");
  const [loadResult, setLoadResult] = useState<ApplyModpackResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const packs = useQuery({
    queryKey: ["modpacks"],
    queryFn: () => api<Modpack[]>("/api/modpacks"),
    enabled: loadOpen,
  });

  const applyPack = useMutation({
    mutationFn: () =>
      api<ApplyModpackResult>(`/api/modpacks/${loadPackId}/apply/${id}`, {
        method: "POST",
        body: JSON.stringify({ mode: loadMode }),
      }),
    onSuccess: async (result) => {
      setLoadResult(result);
      setLoadError(null);
      await queryClient.invalidateQueries({ queryKey: ["server", id] });
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
      void preflight.refetch();
    },
    onError: (error) => {
      setLoadResult(null);
      setLoadError(
        error instanceof ApiError && error.status === 409
          ? "Stop the server before applying a modpack."
          : errText(error, "Apply failed"),
      );
    },
  });

  const openLoadDialog = () => {
    setLoadResult(null);
    setLoadError(null);
    setLoadPackId("");
    setLoadMode("replace");
    setLoadOpen(true);
  };

  /* ------------------------------ render ------------------------------ */

  return (
    <div className="grid gap-6">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            setPackNote(null);
            setPackOpen(true);
          }}
        >
          Snapshot to modpack...
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={dirty}
          title={dirty ? "Save or discard your staged changes first" : undefined}
          onClick={openLoadDialog}
        >
          Load modpack...
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={Boolean(action)}
          onClick={() =>
            run("update-check", () =>
              api(`/api/servers/${id}/mods/update/check`, { method: "POST" }),
            )
          }
        >
          {action === "update-check" ? "Checking..." : "Check updates"}
        </Button>
        <Button
          size="sm"
          disabled={Boolean(action)}
          onClick={() =>
            run("update-apply", () =>
              api(`/api/servers/${id}/mods/update/apply`, { method: "POST" }),
            )
          }
        >
          {action === "update-apply" ? "Queueing..." : "Apply updates"}
        </Button>
        <Button size="sm" variant="outline" onClick={() => preflight.refetch()}>
          Refresh pre-flight
        </Button>
      </div>

      {packNote && (
        <p className="text-xs text-amber-300" role="status">
          Pack created. {packNote}{" "}
          <button
            type="button"
            className="underline underline-offset-2 hover:text-amber-200"
            onClick={() => navigate("/modpacks")}
          >
            Open Modpacks
          </button>
        </p>
      )}

      <section>
        <p className="metric-label">Deployment pre-flight</p>
        {!preflight.isFetched && !preflight.isFetching ? (
          <p className="text-xs text-stone-400">
            Pre-flight has not run yet — press Refresh pre-flight.
          </p>
        ) : preflight.isFetching ? (
          <p>Checking assigned mods and dependencies...</p>
        ) : preflight.isError ? (
          <p className="error">Pre-flight could not be loaded.</p>
        ) : (
          preflight.data && (
            <>
              <Badge tone={tone}>{preflight.data.verdict}</Badge>
              <div className="list mt-2">
                {preflight.data.checks.map((check, index) => (
                  <div className="row" key={`${check.name}-${index}`}>
                    <span className="row-main">
                      <strong>{check.name}</strong>
                      <small>
                        {check.detail}
                        {check.fix ? ` Fix: ${check.fix}` : ""}
                      </small>
                    </span>
                    <Badge
                      tone={
                        check.level === "blocked" || check.level === "error"
                          ? "bad"
                          : check.level === "warn" || check.level === "warning"
                            ? "warn"
                            : "good"
                      }
                    >
                      {check.level}
                    </Badge>
                  </div>
                ))}
              </div>
            </>
          )
        )}
      </section>

      <section className="grid gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="metric-label">Assigned mods ({working.length})</p>
          <div className="flex items-center gap-2">
            {dirty && <Badge tone="warn">Unsaved changes</Badge>}
            <Button
              size="sm"
              variant="ghost"
              disabled={!dirty || save.isPending}
              onClick={discard}
            >
              Discard changes
            </Button>
            <Button
              size="sm"
              disabled={!dirty || save.isPending}
              title={dirty ? undefined : "No unsaved changes"}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving..." : "Save mod set"}
            </Button>
          </div>
        </div>

        {save.isError && <p className="error">{errText(save.error, "Save failed")}</p>}
        {save.isSuccess && !dirty && (
          <p className="text-xs text-emerald-400">Mod set saved. Pre-flight re-run.</p>
        )}
        {(pinMutation.isError || unpinMutation.isError) && (
          <p className="error">
            {errText(pinMutation.error ?? unpinMutation.error, "Pin update failed")}
          </p>
        )}

        {working.length ? (
          <SortableModList
            items={topLevel}
            onReorder={reorderVisible}
            renderName={(mod) => (
              <Link
                to={`/mods/${mod.mod_guid}`}
                className="hover:text-amber-400"
                onClick={(event) => event.stopPropagation()}
              >
                {mod.mod_name ?? mod.mod_guid}
              </Link>
            )}
            renderMeta={(mod) => (
              <span className="flex flex-wrap items-center gap-2 pt-1">
                {!mod.enabled && <Badge tone="neutral">Disabled</Badge>}
                {mod.pinned_version && <Badge tone="warn">Pinned {mod.pinned_version}</Badge>}
                {mod.pinned_reason && (
                  <span className="text-[10px] text-amber-300">{mod.pinned_reason}</span>
                )}
              </span>
            )}
            renderActions={(mod) => {
              const persisted = persistedGuids.has(mod.mod_guid);
              const open = expandedRows.has(mod.mod_guid);
              return (
                <>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-expanded={open}
                    aria-label={open ? "Hide dependencies" : "Show dependencies"}
                    onClick={() => toggleExpanded(mod.mod_guid)}
                  >
                    {open ? "▾" : "▸"}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setEnabled(mod.mod_guid, !mod.enabled)}
                  >
                    {mod.enabled ? "Disable" : "Enable"}
                  </Button>
                  {mod.pinned_version ? (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!persisted || pinBusy}
                      onClick={() => unpinMutation.mutate(mod.mod_guid)}
                    >
                      Unpin
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!persisted || pinBusy}
                      title={persisted ? undefined : "Save the mod set before pinning"}
                      onClick={() => openPinDialog(mod)}
                    >
                      Pin version
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => removeMod(mod.mod_guid)}
                  >
                    Remove
                  </Button>
                </>
              );
            }}
            renderExpanded={(mod) =>
              expandedRows.has(mod.mod_guid) ? (
                <AssignedModExpanded
                  parent={mod}
                  working={working}
                  coverageGuids={coverageByParent.get(mod.mod_guid) ?? EMPTY_GUIDS}
                  explicitGuids={workingGuids}
                  persistedGuids={persistedGuids}
                  pinBusy={pinBusy}
                  onSetEnabled={setEnabled}
                  onOpenPin={openPinDialog}
                  onUnpin={(guid) => unpinMutation.mutate(guid)}
                />
              ) : null
            }
          />
        ) : (
          <Empty label="No mods assigned to this definition." />
        )}
        <p className="text-[10px] text-stone-500">
          Drag the handle to reorder — load order is significant. Pin / unpin apply
          immediately; other edits are staged until you save.
        </p>
      </section>

      <section className="grid gap-3">
        <p className="metric-label">Add mods from the library</p>
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
          <ul className="divide-y divide-stone-800 border border-stone-800">
            {candidates.map((mod) => (
              <AddModRow
                key={mod.guid}
                mod={mod}
                existingGuids={workingGuids}
                onAdd={() => void addMod(mod)}
              />
            ))}
          </ul>
        ) : (
          <Empty label="No library mods match — every candidate is already assigned or filtered out." />
        )}
      </section>

      <Dialog
        open={pinTarget !== null}
        title={`Pin ${pinTarget?.mod_name ?? pinTarget?.mod_guid ?? "mod"}`}
        onClose={() => !pinMutation.isPending && setPinTarget(null)}
      >
        <form className="space-y-4" onSubmit={submitPin}>
          <p className="text-xs leading-5 text-stone-400">
            Holds this mod at a specific version for this server definition. Applied
            immediately — it does not wait for "Save mod set".
          </p>
          <label className="block space-y-1 text-xs text-stone-300">
            Target version
            <Input
              value={pinVersion}
              onChange={(event) => setPinVersion(event.target.value)}
              required
              autoFocus
            />
          </label>
          <label className="block space-y-1 text-xs text-stone-300">
            Reason (optional)
            <Input
              value={pinReason}
              onChange={(event) => setPinReason(event.target.value)}
              maxLength={4096}
              placeholder="Why this version is held"
            />
          </label>
          {pinMutation.isError && (
            <p className="error">{errText(pinMutation.error, "Pin failed")}</p>
          )}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setPinTarget(null)}
              disabled={pinMutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={pinMutation.isPending || !pinVersion.trim()}>
              {pinMutation.isPending ? "Pinning..." : "Pin version"}
            </Button>
          </div>
        </form>
      </Dialog>

      <Dialog
        open={packOpen}
        title="Snapshot this mod set to a modpack"
        onClose={() => !savePack.isPending && setPackOpen(false)}
      >
        <form className="space-y-4" onSubmit={submitPack}>
          <p className="text-xs leading-5 text-stone-400">
            Creates a new reusable modpack from this definition's <strong>last saved</strong> mod
            set and load order. Version pins are not carried into the pack — a pack is a mod list,
            not a version lock.
          </p>
          {dirty && (
            <p className="text-[11px] text-amber-300">
              You have unsaved mod-set changes; the pack captures the last saved state.
            </p>
          )}
          <label className="block space-y-1 text-xs text-stone-300">
            Pack name
            <Input
              value={packName}
              onChange={(event) => setPackName(event.target.value)}
              required
              autoFocus
            />
          </label>
          <label className="block space-y-1 text-xs text-stone-300">
            Description (optional)
            <Input
              value={packDesc}
              onChange={(event) => setPackDesc(event.target.value)}
              placeholder="Operational notes"
            />
          </label>
          {savePack.isError && (
            <p className="error">{errText(savePack.error, "Could not create the pack")}</p>
          )}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setPackOpen(false)}
              disabled={savePack.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={savePack.isPending || !packName.trim()}>
              {savePack.isPending ? "Saving..." : "Create pack"}
            </Button>
          </div>
        </form>
      </Dialog>

      <Dialog
        open={loadOpen}
        title="Load a modpack into this server"
        onClose={() => !applyPack.isPending && setLoadOpen(false)}
      >
        <div className="space-y-4">
          {packs.isLoading ? (
            <p className="text-xs text-stone-400">Loading modpacks...</p>
          ) : packs.isError ? (
            <p className="error">{errText(packs.error, "Could not load modpacks.")}</p>
          ) : packs.data?.length === 0 ? (
            <p className="text-xs leading-5 text-stone-400">
              No modpacks yet — create one with <strong>Snapshot to modpack</strong>.
            </p>
          ) : (
            <>
              <label className="block space-y-1 text-xs text-stone-300">
                <span>Modpack</span>
                <select
                  className="h-10 w-full border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100"
                  value={loadPackId}
                  onChange={(event) => setLoadPackId(event.target.value)}
                >
                  <option value="">Select a modpack...</option>
                  {(packs.data ?? []).map((pack) => (
                    <option key={pack.id} value={String(pack.id)}>
                      {pack.name} ({pack.items.length} mods)
                    </option>
                  ))}
                </select>
              </label>

              <fieldset className="space-y-2">
                <legend className="text-xs text-stone-300">Mode</legend>
                <label className="flex items-center gap-2 text-xs text-stone-300">
                  <input
                    type="radio"
                    name="load-mode"
                    checked={loadMode === "replace"}
                    onChange={() => setLoadMode("replace")}
                  />
                  Replace — clear the server's mod set, then write the pack
                </label>
                <label className="flex items-center gap-2 text-xs text-stone-300">
                  <input
                    type="radio"
                    name="load-mode"
                    checked={loadMode === "append"}
                    onChange={() => setLoadMode("append")}
                  />
                  Append — add the pack's mods, keeping what is already there
                </label>
              </fieldset>

              <p className="text-xs text-stone-400">
                Applies immediately and needs the server stopped. Replace drops version pins on
                mods the pack doesn't contain. All applied mods are set enabled.
              </p>
            </>
          )}

          {loadError && <p className="error">{loadError}</p>}
          {loadResult && (
            <div className="space-y-1 text-xs text-emerald-400">
              <p>
                Applied {loadResult.applied} mod{loadResult.applied === 1 ? "" : "s"}.
              </p>
              {loadResult.dropped_pins.length > 0 && (
                <p className="text-amber-300">
                  These pins were dropped because their mod left the set:{" "}
                  {loadResult.dropped_pins
                    .map((pin) => pin.mod_name ?? pin.mod_guid)
                    .join(", ")}
                </p>
              )}
            </div>
          )}

          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setLoadOpen(false)}
              disabled={applyPack.isPending}
            >
              {loadResult ? "Done" : "Cancel"}
            </Button>
            {packs.data && packs.data.length > 0 && (
              <Button
                type="button"
                onClick={() => applyPack.mutate()}
                disabled={applyPack.isPending || !loadPackId}
              >
                {applyPack.isPending ? "Applying..." : "Apply modpack"}
              </Button>
            )}
          </div>
        </div>
      </Dialog>
    </div>
  );
}

function AddModRow({
  mod,
  existingGuids,
  onAdd,
}: {
  mod: Mod;
  existingGuids: Set<string>;
  onAdd: () => void;
}) {
  const [open, setOpen] = useState(false);
  const detail = useQuery({
    queryKey: ["mod", mod.guid, "deps"],
    queryFn: () => api<ModDeps>(`/api/mods/${mod.guid}`),
    enabled: open,
  });

  const additions = useMemo(() => {
    const nodes = detail.data?.dependency_tree?.nodes ?? [];
    return nodes.filter((node) => node.guid !== mod.guid && !existingGuids.has(node.guid));
  }, [detail.data, mod.guid, existingGuids]);

  return (
    <li className="grid gap-2 px-3 py-2">
      <div className="flex flex-wrap items-center gap-3">
        <span className="min-w-0 flex-1">
          <span className="block truncate font-display text-sm uppercase tracking-wide text-stone-100">
            {mod.name ?? mod.guid}
          </span>
          <span className="flex flex-wrap gap-x-3 text-[10px] text-stone-500">
            <span className="font-mono">{mod.guid}</span>
            <span>{formatSize(mod.size)}</span>
            {!mod.is_local && <span className="text-amber-400">not local</span>}
          </span>
        </span>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          {open ? "Hide deps" : "Deps"}
        </Button>
        <Button size="sm" onClick={onAdd}>
          Add
        </Button>
      </div>
      {open && (
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

/* Expanded subtree under one assigned pick: its resolved dependency closure
 * (read-only links) plus any OTHER explicit picks it covers, rendered with their
 * own enable / pin controls so a covered pick keeps every control it had at the
 * top level. Shares the ["mod", guid, "deps"] cache with AddModRow / the dedup
 * `useQueries` above. */
function AssignedModExpanded({
  parent,
  working,
  coverageGuids,
  explicitGuids,
  persistedGuids,
  pinBusy,
  onSetEnabled,
  onOpenPin,
  onUnpin,
}: {
  parent: WorkingMod;
  working: WorkingMod[];
  coverageGuids: ReadonlySet<string>;
  explicitGuids: Set<string>;
  persistedGuids: Set<string>;
  pinBusy: boolean;
  onSetEnabled: (guid: string, enabled: boolean) => void;
  onOpenPin: (mod: WorkingMod) => void;
  onUnpin: (guid: string) => void;
}) {
  const deps = useQuery({
    queryKey: ["mod", parent.mod_guid, "deps"],
    queryFn: () => api<ModDeps>(`/api/mods/${parent.mod_guid}`),
    staleTime: 60_000,
  });

  const nodes = deps.data?.dependency_tree?.nodes ?? [];
  // Pure dependency nodes: depth > 0 and NOT themselves an explicit pick (those
  // render below as pick-styled rows instead). Sorted by depth then name.
  const pureNodes = [...nodes]
    .filter((node) => node.depth > 0 && !explicitGuids.has(node.guid))
    .sort(
      (a, b) =>
        a.depth - b.depth || (a.name ?? a.guid).localeCompare(b.name ?? b.guid),
    );
  const picks = working.filter((row) => coverageGuids.has(row.mod_guid));

  return (
    <div className="mt-2 w-full space-y-1.5 border-l-2 border-stone-800 pl-3 text-[11px] text-stone-400">
      {picks.map((row) => {
        const persisted = persistedGuids.has(row.mod_guid);
        return (
          <div key={row.mod_guid} className="flex flex-wrap items-center gap-2">
            <Link
              to={`/mods/${row.mod_guid}`}
              className="font-display uppercase tracking-wide text-stone-200 hover:text-amber-400"
              onClick={(event) => event.stopPropagation()}
            >
              {row.mod_name ?? row.mod_guid}
            </Link>
            <Badge tone="neutral">pick</Badge>
            {!row.enabled && <Badge tone="neutral">Disabled</Badge>}
            {row.pinned_version && <Badge tone="warn">Pinned {row.pinned_version}</Badge>}
            {!row.enabled && parent.enabled && (
              <Badge tone="warn">
                loaded as a dependency of {parent.mod_name ?? parent.mod_guid}
              </Badge>
            )}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onSetEnabled(row.mod_guid, !row.enabled)}
            >
              {row.enabled ? "Disable" : "Enable"}
            </Button>
            {row.pinned_version ? (
              <Button
                size="sm"
                variant="outline"
                disabled={!persisted || pinBusy}
                onClick={() => onUnpin(row.mod_guid)}
              >
                Unpin
              </Button>
            ) : (
              <Button
                size="sm"
                variant="outline"
                disabled={!persisted || pinBusy}
                title={persisted ? undefined : "Save the mod set before pinning"}
                onClick={() => onOpenPin(row)}
              >
                Pin version
              </Button>
            )}
          </div>
        );
      })}

      {deps.isLoading ? (
        <p>...</p>
      ) : deps.isError ? (
        <p className="error">Dependency lookup failed.</p>
      ) : pureNodes.length ? (
        pureNodes.map((node) => (
          <div
            key={`${node.guid}-${node.depth}`}
            className="flex flex-wrap items-center gap-2"
          >
            {node.state === "unresolved" ? (
              <>
                <span className="font-mono text-stone-500">{node.guid}</span>
                <Badge tone="warn">unresolved</Badge>
              </>
            ) : (
              <>
                <Link
                  to={`/mods/${node.guid}`}
                  className="text-stone-300 hover:text-amber-400"
                  onClick={(event) => event.stopPropagation()}
                >
                  {node.name ?? node.guid}
                </Link>
                <span className="text-stone-500">via {node.via}</span>
              </>
            )}
          </div>
        ))
      ) : picks.length ? null : (
        <p>No dependencies.</p>
      )}
    </div>
  );
}
