import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Empty } from "../components/Empty";
import { PageHeading } from "../components/PageHeading";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Dialog, Input } from "../components/ui";
import { api, apiVoid, ApiError } from "../lib/api";

type ApiState = "ok" | "not_found" | "unchecked";
type ModRecord = {
  guid: string;
  name: string | null;
  summary: string | null;
  installed_version: string | null;
  latest_version: string | null;
  latest_game_version: string | null;
  size: number | null;
  tags: unknown[] | null;
  is_unlisted: boolean;
  is_private: boolean;
  is_obsolete: boolean;
  is_local: boolean;
  api_state: ApiState;
  pinned_version: string | null;
  pinned_at_build: string | null;
  pinned_reason: string | null;
  has_update: boolean;
  stale_pin: boolean;
  required_by?: { guid: string; name: string | null }[];
  // Present only when the list is fetched with `?refs=1`; guard with `?? undefined`.
  cache_bytes?: number | null;
  is_orphan?: boolean;
  is_unreferenced?: boolean;
  kept_by?: string[] | null;
};
type ModReferences = {
  servers: string[];
  modpacks: string[];
  required_by: { guid: string; name: string | null }[];
};
// Recovered from the now-deleted Storage.tsx — only the fields this page renders.
type StorageReport = {
  mods_path: string;
  free_bytes: number;
  total_bytes: number;
};
type EnqueuedJob = { job_id: number; kind: string };
type SearchResult = {
  id: string;
  name: string | null;
  summary: string | null;
  latest_version: string | null;
  workshop_url: string | null;
};
type FilterState = "all" | ApiState;
type SortCol = "name" | "guid" | "installed" | "latest" | "cache" | "orphan" | "state";
type SortState = { col: SortCol; dir: "asc" | "desc" } | null;

// Recovered verbatim from the deleted Storage.tsx.
const formatSize = (bytes: number) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
};

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

const addErrorMessage = (error: unknown) => {
  if (error instanceof ApiError) {
    if (error.status === 502) return "The Workshop API is unreachable — try again shortly.";
    if (error.status === 404) return `${error.message} Check the URL or GUID.`;
  }
  return errorMessage(error);
};

const availabilityOf = (mod: ModRecord) =>
  mod.api_state === "not_found"
    ? "Workshop unavailable"
    : mod.is_obsolete
      ? "Obsolete"
      : mod.is_private
        ? "Private"
        : mod.is_unlisted
          ? "Unlisted"
          : mod.api_state === "unchecked"
            ? "Unchecked"
            : "Available";

const availabilityToneOf = (mod: ModRecord): "good" | "warn" | "bad" =>
  mod.api_state === "not_found" || mod.is_private || mod.is_obsolete
    ? "bad"
    : mod.is_unlisted || mod.api_state === "unchecked"
      ? "warn"
      : "good";

const orphanText = (mod: ModRecord) =>
  mod.is_orphan
    ? "Orphan"
    : mod.is_unreferenced
      ? "Stale row"
      : mod.kept_by?.length
        ? `Kept: ${mod.kept_by.join(", ")}`
        : "-";

const orphanTone = (mod: ModRecord): "warn" | "neutral" => (mod.is_orphan ? "warn" : "neutral");

const sortKey = (mod: ModRecord, col: SortCol): string | number | null => {
  switch (col) {
    case "name":
      return mod.name;
    case "guid":
      return mod.guid;
    case "installed":
      return mod.installed_version;
    case "latest":
      return mod.latest_version;
    case "cache":
      return mod.cache_bytes ?? null;
    case "orphan": {
      const text = orphanText(mod);
      return text === "-" ? null : text;
    }
    case "state":
      return availabilityOf(mod);
    default:
      return null;
  }
};

const sortRows = (rows: ModRecord[], sort: SortState): ModRecord[] => {
  if (!sort) return rows;
  const { dir } = sort;
  return rows
    .map((row, index) => ({ row, index, key: sortKey(row, sort.col) }))
    .sort((a, b) => {
      const aNull = a.key === null || a.key === "";
      const bNull = b.key === null || b.key === "";
      if (aNull && bNull) return a.index - b.index;
      if (aNull) return 1; // nulls always last, regardless of direction
      if (bNull) return -1;
      let cmp: number;
      if (typeof a.key === "number" && typeof b.key === "number") cmp = a.key - b.key;
      else cmp = String(a.key).localeCompare(String(b.key), undefined, { numeric: true, sensitivity: "base" });
      return dir === "asc" ? cmp : -cmp;
    })
    .map((entry) => entry.row);
};

const COLUMNS: { col: SortCol; label: string }[] = [
  { col: "name", label: "Name" },
  { col: "guid", label: "GUID" },
  { col: "installed", label: "Installed" },
  { col: "latest", label: "Latest" },
  { col: "cache", label: "Cache size" },
  { col: "orphan", label: "Orphan" },
  { col: "state", label: "State" },
];

export function ModsPage() {
  const queryClient = useQueryClient();
  const addInputRef = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState("");
  const [local, setLocal] = useState("all");
  const [state, setState] = useState<FilterState>("all");
  const [updatesOnly, setUpdatesOnly] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [pinTarget, setPinTarget] = useState<ModRecord | null>(null);
  const [pinVersion, setPinVersion] = useState("");
  const [pinReason, setPinReason] = useState("");
  const [addInput, setAddInput] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [wsQuery, setWsQuery] = useState("");
  const [sort, setSort] = useState<SortState>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [diskTarget, setDiskTarget] = useState<ModRecord | null>(null);
  const [libraryTarget, setLibraryTarget] = useState<ModRecord | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const filters = new URLSearchParams();
  if (search.trim()) filters.set("q", search.trim());
  if (local !== "all") filters.set("local", local);
  if (state !== "all") filters.set("state", state);
  if (updatesOnly) filters.set("update", "true");
  const filterString = filters.toString();
  const modsQuery = useQuery({
    queryKey: ["mods", filterString],
    queryFn: () => api<ModRecord[]>(`/api/mods${filterString ? `?${filterString}&refs=1` : "?refs=1"}`),
  });

  const storageQuery = useQuery({
    queryKey: ["storage"],
    queryFn: () => api<StorageReport>("/api/storage"),
  });

  const wsSearchQuery = useQuery({
    queryKey: ["mods-search", wsQuery],
    queryFn: () => api<SearchResult[]>(`/api/mods/search?q=${encodeURIComponent(wsQuery)}`),
    enabled: wsQuery.length > 0,
  });

  const refTarget = diskTarget ?? libraryTarget;
  const referencesQuery = useQuery({
    queryKey: ["mod-references", refTarget?.guid],
    queryFn: () => api<ModReferences>(`/api/mods/${refTarget!.guid}/references`),
    enabled: refTarget !== null,
  });

  const invalidateMods = () => queryClient.invalidateQueries({ queryKey: ["mods"] });
  const invalidateStorage = () => queryClient.invalidateQueries({ queryKey: ["storage"] });
  const runJob = (path: string, body?: unknown) =>
    api<EnqueuedJob>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  const jobMutation = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => runJob(path, body),
    onSuccess: (job) => {
      setNotice(`${job.kind.replaceAll("_", " ")} queued as job #${job.job_id}.`);
      void invalidateMods();
    },
    onError: (error) => setNotice(errorMessage(error)),
  });
  const pinMutation = useMutation({
    mutationFn: ({ mod, version, reason }: { mod: ModRecord; version: string; reason: string }) =>
      api<ModRecord>(`/api/mods/${mod.guid}/pin`, {
        method: "POST",
        body: JSON.stringify({ version, reason: reason || null }),
      }),
    onSuccess: (mod) => {
      setNotice(`${mod.name ?? mod.guid} pinned to ${mod.pinned_version}.`);
      setPinTarget(null);
      void invalidateMods();
    },
    onError: (error) => setNotice(errorMessage(error)),
  });
  const unpinMutation = useMutation({
    mutationFn: (mod: ModRecord) => api<ModRecord>(`/api/mods/${mod.guid}/pin`, { method: "DELETE" }),
    onSuccess: (mod) => {
      setNotice(`${mod.name ?? mod.guid} is no longer pinned.`);
      void invalidateMods();
    },
    onError: (error) => setNotice(errorMessage(error)),
  });
  const addMutation = useMutation({
    mutationFn: (source: string) =>
      api<ModRecord>("/api/mods/add", {
        method: "POST",
        body: JSON.stringify({ url_or_id: source }),
      }),
    onSuccess: (mod) => {
      setNotice(`${mod.name ?? mod.guid} added to the library.`);
      setAddInput("");
      setAddError(null);
      void invalidateMods();
      void invalidateStorage();
    },
    onError: (error) => setAddError(addErrorMessage(error)),
  });
  const removeLocalMutation = useMutation({
    mutationFn: (mod: ModRecord) => apiVoid(`/api/mods/${mod.guid}/local`, { method: "DELETE" }),
    onSuccess: (_result, mod) => {
      setNotice(`${mod.name ?? mod.guid} removed from disk; the library row is kept.`);
      setDiskTarget(null);
      setDeleteError(null);
      void invalidateMods();
      void invalidateStorage();
    },
    onError: (error) => {
      // 409 (and any other failure) surfaces inline in the still-open dialog.
      const message = errorMessage(error);
      setDeleteError(message);
      setNotice(message);
    },
  });
  const removeLibraryMutation = useMutation({
    mutationFn: (mod: ModRecord) => apiVoid(`/api/mods/${mod.guid}`, { method: "DELETE" }),
    onSuccess: (_result, mod) => {
      setNotice(`${mod.name ?? mod.guid} removed from the library.`);
      setLibraryTarget(null);
      setDeleteError(null);
      void invalidateMods();
      void invalidateStorage();
    },
    onError: (error) => {
      const message = errorMessage(error);
      setDeleteError(message);
      setNotice(message);
    },
  });

  const openPinDialog = (mod: ModRecord) => {
    setPinTarget(mod);
    setPinVersion(mod.latest_version ?? mod.installed_version ?? "");
    setPinReason(mod.pinned_reason ?? "");
  };
  const submitPin = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!pinTarget || !pinVersion.trim()) return;
    pinMutation.mutate({ mod: pinTarget, version: pinVersion.trim(), reason: pinReason.trim() });
  };
  const submitAdd = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const source = addInput.trim();
    if (!source) return;
    addMutation.mutate(source);
  };
  const focusAddCard = () => {
    addInputRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    addInputRef.current?.focus();
  };
  const toggleExpand = (guid: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(guid)) next.delete(guid);
      else next.add(guid);
      return next;
    });
  const cycleSort = (col: SortCol) =>
    setSort((prev) => {
      if (!prev || prev.col !== col) return { col, dir: "asc" };
      if (prev.dir === "asc") return { col, dir: "desc" };
      return null;
    });
  const openDiskDialog = (mod: ModRecord) => {
    setDeleteError(null);
    setLibraryTarget(null);
    setDiskTarget(mod);
  };
  const openLibraryDialog = (mod: ModRecord) => {
    setDeleteError(null);
    setDiskTarget(null);
    setLibraryTarget(mod);
  };

  const busy = jobMutation.isPending;
  const rowBusy =
    busy ||
    pinMutation.isPending ||
    unpinMutation.isPending ||
    removeLocalMutation.isPending ||
    removeLibraryMutation.isPending;
  const rows = sortRows(modsQuery.data ?? [], sort);

  const renderReferences = () => {
    if (!refTarget) return null;
    if (referencesQuery.isLoading) return <p className="text-[11px] text-stone-500">Checking references…</p>;
    if (referencesQuery.isError)
      return (
        <p className="text-[11px] text-amber-300">
          Could not check references: {errorMessage(referencesQuery.error)}
        </p>
      );
    const data = referencesQuery.data;
    if (!data) return null;
    if (!data.servers.length && !data.modpacks.length && !data.required_by.length)
      return (
        <p className="text-[11px] text-emerald-300">
          Nothing references this mod — no server definition, modpack, or dependent mod.
        </p>
      );
    return (
      <div className="space-y-1 border border-stone-700 bg-stone-900/60 p-2 text-[11px] text-stone-300">
        {data.servers.length ? (
          <p>
            Server definitions: <span className="text-amber-300">{data.servers.join(", ")}</span>
          </p>
        ) : null}
        {data.modpacks.length ? (
          <p>
            Modpacks: <span className="text-amber-300">{data.modpacks.join(", ")}</span>
          </p>
        ) : null}
        {data.required_by.length ? (
          <p>
            Required by:{" "}
            <span className="text-amber-300">
              {data.required_by.map((ref) => ref.name ?? ref.guid).join(", ")}
            </span>
          </p>
        ) : null}
        <p className="text-stone-500">The delete is refused while any of these hold a reference.</p>
      </div>
    );
  };

  return (
    <>
      <PageHeading
        title="Mod Library"
        detail="Disk inventory, Workshop availability, updates, repair, and known-good version pins."
        actions={
          <>
            <Button onClick={focusAddCard}>Add mod</Button>
            <Button variant="outline" onClick={() => jobMutation.mutate({ path: "/api/mods/scan" })} disabled={busy}>
              {busy ? "Queueing..." : "Scan cache"}
            </Button>
            <Button onClick={() => jobMutation.mutate({ path: "/api/mods/updates/check" })} disabled={busy}>
              Check updates
            </Button>
            <Button
              variant="outline"
              onClick={() => jobMutation.mutate({ path: "/api/mods/updates/apply" })}
              disabled={busy}
            >
              Apply all updates
            </Button>
          </>
        }
      />
      <Card className="mb-4">
        <CardHeader>
          <CardTitle>Addon Cache Free Space</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {storageQuery.isLoading && <p className="text-sm text-stone-400">Reading storage usage...</p>}
          {storageQuery.isError && (
            <div className="space-y-2">
              <p className="error">Could not load storage: {errorMessage(storageQuery.error)}</p>
              <Button size="sm" variant="outline" onClick={() => void storageQuery.refetch()}>
                Retry
              </Button>
            </div>
          )}
          {storageQuery.data && (
            <>
              <FreeSpaceBar freeBytes={storageQuery.data.free_bytes} totalBytes={storageQuery.data.total_bytes} />
              <p className="text-[11px] text-stone-400">
                Mods directory:{" "}
                <span className="font-mono text-stone-300">{storageQuery.data.mods_path}</span>
              </p>
            </>
          )}
        </CardContent>
      </Card>
      <Card className="mb-4">
        <CardContent className="flex flex-col gap-3 pt-4">
          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_10rem_10rem_auto]">
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search installed names..."
              aria-label="Search mods"
            />
            <select
              className="h-10 border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100"
              value={local}
              onChange={(event) => setLocal(event.target.value)}
              aria-label="Local availability filter"
            >
              <option value="all">All sources</option>
              <option value="true">Installed</option>
              <option value="false">Not local</option>
            </select>
            <select
              className="h-10 border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100"
              value={state}
              onChange={(event) => setState(event.target.value as FilterState)}
              aria-label="Workshop state filter"
            >
              <option value="all">All states</option>
              <option value="ok">Available</option>
              <option value="not_found">Unavailable</option>
              <option value="unchecked">Unchecked</option>
            </select>
            <label className="flex h-10 items-center gap-2 whitespace-nowrap text-xs text-stone-300">
              <input
                type="checkbox"
                checked={updatesOnly}
                onChange={(event) => setUpdatesOnly(event.target.checked)}
              />
              Updates only
            </label>
          </div>
          <div className="flex flex-wrap gap-2 border-t border-stone-800 pt-3">
            <Button
              size="sm"
              variant="outline"
              onClick={() => jobMutation.mutate({ path: "/api/mods/verify" })}
              disabled={busy}
            >
              Verify and repair library
            </Button>
            {notice && (
              <p className="self-center text-xs text-stone-300" role="status">
                {notice}
              </p>
            )}
          </div>
        </CardContent>
      </Card>
      <Card className="mb-4">
        <CardHeader>
          <CardTitle>Add mod</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <form className="flex flex-wrap gap-2" onSubmit={submitAdd}>
            <Input
              ref={addInputRef}
              className="min-w-0 flex-1"
              value={addInput}
              onChange={(event) => setAddInput(event.target.value)}
              placeholder="Workshop URL or 16-hex mod GUID..."
              aria-label="Add mod by Workshop URL or GUID"
            />
            <Button type="submit" disabled={!addInput.trim() || addMutation.isPending}>
              {addMutation.isPending ? "Adding..." : "Add mod"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={!addInput.trim() || wsSearchQuery.isFetching}
              onClick={() => setWsQuery(addInput.trim())}
            >
              {wsSearchQuery.isFetching ? "Searching..." : "Search mod"}
            </Button>
          </form>
          {addError && <p className="error">{addError}</p>}
          {wsSearchQuery.isError && (
            <div className="space-y-2">
              <p className="error">Workshop search failed: {errorMessage(wsSearchQuery.error)}</p>
              <Button size="sm" variant="outline" onClick={() => void wsSearchQuery.refetch()}>
                Retry
              </Button>
            </div>
          )}
          {wsSearchQuery.isSuccess && wsSearchQuery.data?.length === 0 && (
            <Empty label={`No Workshop results for "${wsQuery}".`} />
          )}
          {wsSearchQuery.data?.length ? (
            <div className="divide-y divide-stone-800">
              {wsSearchQuery.data.map((result) => (
                <SearchResultRow
                  key={result.id}
                  result={result}
                  busy={addMutation.isPending}
                  onAdd={() => addMutation.mutate(result.id)}
                />
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Addon Inventory {modsQuery.data ? `(${modsQuery.data.length})` : ""}</CardTitle>
        </CardHeader>
        <CardContent>
          {modsQuery.isLoading && <p className="text-sm text-stone-400">Loading mod library...</p>}
          {modsQuery.isError && (
            <div className="space-y-3">
              <p className="error">Could not load the mod library: {errorMessage(modsQuery.error)}</p>
              <Button size="sm" variant="outline" onClick={() => void modsQuery.refetch()}>
                Retry
              </Button>
            </div>
          )}
          {!modsQuery.isLoading && !modsQuery.isError && !rows.length && (
            <Empty label="No mods match these filters. Scan the local cache to import installed addons." />
          )}
          {rows.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs text-stone-300">
                <thead>
                  <tr className="border-b border-stone-700 text-left text-[10px] uppercase tracking-widest text-stone-500">
                    {COLUMNS.map(({ col, label }) => (
                      <th key={col} className="py-2 pr-4 font-semibold">
                        <button
                          type="button"
                          onClick={() => cycleSort(col)}
                          className="inline-flex items-center gap-1 uppercase tracking-widest hover:text-stone-200"
                        >
                          {label}
                          {sort?.col === col && <span aria-hidden>{sort.dir === "asc" ? "▲" : "▼"}</span>}
                        </button>
                      </th>
                    ))}
                    <th className="py-2 pr-4 font-semibold">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-stone-800">
                  {rows.map((mod) => {
                    const open = expanded.has(mod.guid);
                    const oText = orphanText(mod);
                    return (
                      <Fragment key={mod.guid}>
                        <tr className="align-top">
                          <td className="py-2 pr-4">
                            <div className="flex items-center gap-2 whitespace-nowrap">
                              <button
                                type="button"
                                onClick={() => toggleExpand(mod.guid)}
                                className="text-left font-semibold text-stone-100 hover:text-amber-400"
                                aria-expanded={open}
                              >
                                {mod.name ?? mod.guid}
                              </button>
                              {mod.has_update && <Badge tone="warn">Update</Badge>}
                              {mod.stale_pin && <Badge tone="bad">Stale pin</Badge>}
                              {mod.pinned_version && !mod.stale_pin && (
                                <Badge tone="neutral">Pinned {mod.pinned_version}</Badge>
                              )}
                            </div>
                          </td>
                          <td className="py-2 pr-4">
                            <Link
                              to={`/mods/${mod.guid}`}
                              className="font-mono text-[10px] text-stone-500 hover:text-amber-400"
                            >
                              {mod.guid}
                            </Link>
                          </td>
                          <td className="whitespace-nowrap py-2 pr-4">{mod.installed_version ?? "—"}</td>
                          <td className="whitespace-nowrap py-2 pr-4">{mod.latest_version ?? "—"}</td>
                          <td className="whitespace-nowrap py-2 pr-4 font-mono">
                            {mod.cache_bytes == null ? "-" : formatSize(mod.cache_bytes)}
                          </td>
                          <td className="whitespace-nowrap py-2 pr-4">
                            {oText === "-" ? (
                              <span className="text-stone-500">-</span>
                            ) : (
                              <Badge tone={orphanTone(mod)}>{oText}</Badge>
                            )}
                          </td>
                          <td className="whitespace-nowrap py-2 pr-4">
                            <Badge tone={availabilityToneOf(mod)}>{availabilityOf(mod)}</Badge>
                          </td>
                          <td className="py-2 pr-4">
                            <div className="flex flex-wrap gap-1.5">
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => jobMutation.mutate({ path: `/api/mods/${mod.guid}/download` })}
                                disabled={rowBusy}
                              >
                                {mod.is_local ? "Re-download" : "Download"}
                              </Button>
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() =>
                                  jobMutation.mutate({ path: "/api/mods/verify", body: { guids: [mod.guid] } })
                                }
                                disabled={rowBusy || !mod.is_local}
                              >
                                Verify
                              </Button>
                              {mod.pinned_version ? (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() => unpinMutation.mutate(mod)}
                                  disabled={rowBusy}
                                >
                                  Unpin
                                </Button>
                              ) : (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() => openPinDialog(mod)}
                                  disabled={rowBusy || (!mod.latest_version && !mod.installed_version)}
                                >
                                  Pin version
                                </Button>
                              )}
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => openDiskDialog(mod)}
                                disabled={rowBusy || !mod.is_local}
                              >
                                Remove from disk
                              </Button>
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => openLibraryDialog(mod)}
                                disabled={rowBusy}
                              >
                                Remove from library
                              </Button>
                            </div>
                          </td>
                        </tr>
                        {open && (
                          <tr className="bg-stone-950/40">
                            <td colSpan={8} className="px-4 py-3">
                              <div className="space-y-2 text-[11px] leading-5 text-stone-400">
                                {mod.summary && <p>{mod.summary}</p>}
                                <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-stone-500">
                                  <span>{mod.is_local ? "In local cache" : "Not in local cache"}</span>
                                  <span>
                                    Reported size: {mod.size == null ? "unknown" : formatSize(mod.size)}
                                  </span>
                                  {mod.latest_game_version && <span>Game: {mod.latest_game_version}</span>}
                                </div>
                                {mod.pinned_reason && (
                                  <p className="text-amber-300">
                                    Pin reason: {mod.pinned_reason}
                                    {mod.pinned_at_build ? ` (engine build ${mod.pinned_at_build})` : ""}
                                  </p>
                                )}
                                {mod.required_by?.length ? (
                                  <p>
                                    Required by{" "}
                                    {mod.required_by.map((ref) => ref.name ?? ref.guid).join(", ")} — deleting
                                    it is blocked while those mods are assigned or packed.
                                  </p>
                                ) : null}
                                {mod.tags?.length ? (
                                  <div className="flex flex-wrap gap-1">
                                    {mod.tags.slice(0, 6).map((tag, index) => (
                                      <Badge key={`${String(tag)}-${index}`}>{String(tag)}</Badge>
                                    ))}
                                  </div>
                                ) : null}
                                <Link
                                  to={`/mods/${mod.guid}`}
                                  className="inline-block font-semibold uppercase tracking-wider text-amber-400 underline-offset-4 hover:underline"
                                >
                                  Open mod detail
                                </Link>
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
        </CardContent>
      </Card>
      <Dialog
        open={pinTarget !== null}
        title={`Pin ${pinTarget?.name ?? pinTarget?.guid ?? "mod"}`}
        onClose={() => !pinMutation.isPending && setPinTarget(null)}
      >
        <form className="space-y-4" onSubmit={submitPin}>
          <p className="text-xs leading-5 text-stone-400">
            Pins hold this library mod at a known-good version. They become stale after an engine build changes.
          </p>
          <label className="block space-y-1 text-xs text-stone-300">
            Target version
            <Input value={pinVersion} onChange={(event) => setPinVersion(event.target.value)} required autoFocus />
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
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setPinTarget(null)} disabled={pinMutation.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={pinMutation.isPending || !pinVersion.trim()}>
              {pinMutation.isPending ? "Pinning..." : "Pin version"}
            </Button>
          </div>
        </form>
      </Dialog>
      <Dialog
        open={diskTarget !== null}
        title={`Remove ${diskTarget?.name ?? diskTarget?.guid ?? "mod"} from disk?`}
        onClose={() => !removeLocalMutation.isPending && setDiskTarget(null)}
      >
        <div className="space-y-4">
          <p className="text-xs leading-5 text-stone-400">
            This deletes the addon files for <b className="text-stone-200">{diskTarget?.guid}</b> from the local
            cache. The library row is kept, so the mod can be re-downloaded later. Deletion is refused while any
            server is running.
          </p>
          {renderReferences()}
          {deleteError && <p className="error">{deleteError}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDiskTarget(null)}
              disabled={removeLocalMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={removeLocalMutation.isPending}
              onClick={() => diskTarget && removeLocalMutation.mutate(diskTarget)}
            >
              {removeLocalMutation.isPending ? "Removing..." : "Remove files"}
            </Button>
          </div>
        </div>
      </Dialog>
      <Dialog
        open={libraryTarget !== null}
        title={`Remove ${libraryTarget?.name ?? libraryTarget?.guid ?? "mod"} from the library?`}
        onClose={() => !removeLibraryMutation.isPending && setLibraryTarget(null)}
      >
        <div className="space-y-4">
          <p className="text-xs leading-5 text-stone-400">
            Deletes the library row for <b className="text-stone-200">{libraryTarget?.guid}</b> — its version
            cache, dependency records, and scenarios.
            {libraryTarget?.is_local
              ? " The on-disk addon files are deleted too. This is refused while a server is running."
              : " Nothing is on disk to remove."}{" "}
            The delete is refused if the mod is still referenced by a server definition, modpack, or resolved
            dependency. Add it again by Workshop URL/ID to restore it.
          </p>
          {renderReferences()}
          {deleteError && <p className="error">{deleteError}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setLibraryTarget(null)}
              disabled={removeLibraryMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={removeLibraryMutation.isPending}
              onClick={() => libraryTarget && removeLibraryMutation.mutate(libraryTarget)}
            >
              {removeLibraryMutation.isPending ? "Removing..." : "Remove entry"}
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}

function FreeSpaceBar({ freeBytes, totalBytes }: { freeBytes: number; totalBytes: number }) {
  const used = Math.max(0, totalBytes - freeBytes);
  const usedPct = totalBytes > 0 ? Math.min(100, (used / totalBytes) * 100) : 0;
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[11px] text-stone-400">
        <span>
          Used: <b className="text-stone-200">{formatSize(used)}</b>
        </span>
        <span>
          Free: <b className="text-stone-200">{formatSize(freeBytes)}</b>
        </span>
        <span>Total: {formatSize(totalBytes)}</span>
      </div>
      <div className="h-3 w-full border border-stone-700 bg-stone-950">
        <div
          className="h-full bg-amber-500"
          style={{ width: `${usedPct.toFixed(2)}%` }}
          role="progressbar"
          aria-valuenow={Math.round(usedPct)}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      </div>
    </div>
  );
}

function SearchResultRow({
  result,
  busy,
  onAdd,
}: {
  result: SearchResult;
  busy: boolean;
  onAdd: () => void;
}) {
  return (
    <article className="grid gap-3 py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          {result.workshop_url ? (
            <a
              href={result.workshop_url}
              target="_blank"
              rel="noopener noreferrer"
              className="font-display text-base font-bold uppercase tracking-wide text-stone-100 hover:text-amber-400"
            >
              {result.name ?? result.id}
            </a>
          ) : (
            <strong className="font-display text-base font-bold uppercase tracking-wide text-stone-100">
              {result.name ?? result.id}
            </strong>
          )}
        </div>
        {result.summary && <p className="text-xs leading-5 text-stone-400">{result.summary}</p>}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-stone-400">
          <span className="font-mono text-stone-500">{result.id}</span>
          <span>
            Latest: <b className="text-stone-200">{result.latest_version ?? "unknown"}</b>
          </span>
          {result.workshop_url && (
            <a
              href={result.workshop_url}
              target="_blank"
              rel="noopener noreferrer"
              className="font-semibold uppercase tracking-wider text-amber-400 underline-offset-4 hover:underline"
            >
              Workshop page
            </a>
          )}
        </div>
      </div>
      <div className="flex flex-wrap content-start gap-2 lg:justify-end">
        <Button size="sm" onClick={onAdd} disabled={busy}>
          Add
        </Button>
      </div>
    </article>
  );
}
