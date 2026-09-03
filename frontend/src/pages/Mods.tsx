import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Empty } from "../components/Empty";
import { PageHeading } from "../components/PageHeading";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Dialog, Input } from "../components/ui";
import { api, ApiError } from "../lib/api";

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

const formatSize = (bytes: number | null) => {
  if (bytes === null) return "Unknown size";
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

export function ModsPage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [local, setLocal] = useState("all");
  const [state, setState] = useState<FilterState>("all");
  const [updatesOnly, setUpdatesOnly] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [pinTarget, setPinTarget] = useState<ModRecord | null>(null);
  const [pinVersion, setPinVersion] = useState("");
  const [pinReason, setPinReason] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [addInput, setAddInput] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [wsInput, setWsInput] = useState("");
  const [wsQuery, setWsQuery] = useState("");

  const filters = new URLSearchParams();
  if (search.trim()) filters.set("q", search.trim());
  if (local !== "all") filters.set("local", local);
  if (state !== "all") filters.set("state", state);
  if (updatesOnly) filters.set("update", "true");
  const filterString = filters.toString();
  const modsQuery = useQuery({
    queryKey: ["mods", filterString],
    queryFn: () => api<ModRecord[]>(`/api/mods${filterString ? `?${filterString}` : ""}`),
  });

  const wsSearchQuery = useQuery({
    queryKey: ["mods-search", wsQuery],
    queryFn: () => api<SearchResult[]>(`/api/mods/search?q=${encodeURIComponent(wsQuery)}`),
    enabled: wsQuery.length > 0,
  });

  const invalidateMods = () => queryClient.invalidateQueries({ queryKey: ["mods"] });
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
      setAddOpen(false);
      setAddInput("");
      setAddError(null);
      void invalidateMods();
    },
    onError: (error) => setAddError(addErrorMessage(error)),
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
  const openAddDialog = () => {
    setAddError(null);
    setAddOpen(true);
  };
  const closeAddDialog = () => {
    if (addMutation.isPending) return;
    setAddOpen(false);
    setAddError(null);
  };
  const submitAdd = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const source = addInput.trim();
    if (!source) return;
    addMutation.mutate(source);
  };
  const submitWsSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const q = wsInput.trim();
    if (!q) return;
    setWsQuery(q);
  };

  const busy = jobMutation.isPending;
  return (
    <>
      <PageHeading
        title="Mod Library"
        detail="Disk inventory, Workshop availability, updates, repair, and known-good version pins."
        actions={
          <>
            <Button onClick={openAddDialog}>Add mod</Button>
            <Button variant="outline" onClick={() => jobMutation.mutate({ path: "/api/mods/scan" })} disabled={busy}>
              {busy ? "Queueing..." : "Scan cache"}
            </Button>
            <Button onClick={() => jobMutation.mutate({ path: "/api/mods/updates/check" })} disabled={busy}>
              Check updates
            </Button>
          </>
        }
      />
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
              onClick={() => jobMutation.mutate({ path: "/api/mods/updates/apply" })}
              disabled={busy}
            >
              Apply all updates
            </Button>
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
          <CardTitle>Workshop Search</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <form className="flex flex-wrap gap-2" onSubmit={submitWsSearch}>
            <Input
              className="min-w-0 flex-1"
              value={wsInput}
              onChange={(event) => setWsInput(event.target.value)}
              placeholder="Search the Workshop for a mod to add..."
              aria-label="Workshop search"
            />
            <Button type="submit" disabled={!wsInput.trim() || wsSearchQuery.isFetching}>
              {wsSearchQuery.isFetching ? "Searching..." : "Search"}
            </Button>
          </form>
          {addError && !addOpen && <p className="error">{addError}</p>}
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
          {!modsQuery.isLoading &&
            !modsQuery.isError &&
            !modsQuery.data?.length && (
              <Empty label="No mods match these filters. Scan the local cache to import installed addons." />
            )}
          {modsQuery.data?.length ? (
            <div className="divide-y divide-stone-800">
              {modsQuery.data.map((mod) => (
                <ModRow
                  key={mod.guid}
                  mod={mod}
                  busy={busy || pinMutation.isPending || unpinMutation.isPending}
                  onPin={() => openPinDialog(mod)}
                  onUnpin={() => unpinMutation.mutate(mod)}
                  onVerify={() => jobMutation.mutate({ path: "/api/mods/verify", body: { guids: [mod.guid] } })}
                  onDownload={() => jobMutation.mutate({ path: `/api/mods/${mod.guid}/download` })}
                />
              ))}
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
      <Dialog open={addOpen} title="Add mod" onClose={closeAddDialog}>
        <form className="space-y-4" onSubmit={submitAdd}>
          <p className="text-xs leading-5 text-stone-400">
            Paste a Workshop URL or a bare 16-hex mod GUID. The mod is added to the library and enriched from the
            Workshop.
          </p>
          <label className="block space-y-1 text-xs text-stone-300">
            Workshop URL or mod GUID
            <Input
              value={addInput}
              onChange={(event) => setAddInput(event.target.value)}
              required
              autoFocus
              placeholder="https://steamcommunity.com/sharedfiles/filedetails/?id=… or 16-hex GUID"
            />
          </label>
          {addError && <p className="error">{addError}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeAddDialog} disabled={addMutation.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={addMutation.isPending || !addInput.trim()}>
              {addMutation.isPending ? "Adding..." : "Add mod"}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}

function ModRow({
  mod,
  busy,
  onPin,
  onUnpin,
  onVerify,
  onDownload,
}: {
  mod: ModRecord;
  busy: boolean;
  onPin: () => void;
  onUnpin: () => void;
  onVerify: () => void;
  onDownload: () => void;
}) {
  const availability = mod.api_state === "not_found"
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
  const availabilityTone =
    mod.api_state === "not_found" || mod.is_private || mod.is_obsolete
      ? "bad"
      : mod.is_unlisted || mod.api_state === "unchecked"
        ? "warn"
        : "good";
  return (
    <article className="grid gap-3 py-4 lg:grid-cols-[minmax(0,1fr)_auto]">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Link
            to={`/mods/${mod.guid}`}
            className="font-display text-lg uppercase tracking-wide text-stone-100 hover:text-amber-400"
          >
            {mod.name ?? mod.guid}
          </Link>
          <Badge tone={availabilityTone}>{availability}</Badge>
          {mod.has_update && <Badge tone="warn">Update available</Badge>}
          {mod.stale_pin && <Badge tone="bad">Stale pin</Badge>}
          {mod.pinned_version && !mod.stale_pin && <Badge tone="neutral">Pinned {mod.pinned_version}</Badge>}
          {mod.required_by?.length ? <Badge tone="neutral">Dependency</Badge> : null}
        </div>
        {mod.summary && <p className="text-xs leading-5 text-stone-400">{mod.summary}</p>}
        {mod.required_by?.length ? (
          <p className="text-[11px] text-stone-400">
            Required by {mod.required_by.map((ref) => ref.name ?? ref.guid).join(", ")} — deleting it
            is blocked while those mods are assigned or packed.
          </p>
        ) : null}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-stone-400">
          <span className="font-mono text-stone-500">{mod.guid}</span>
          <span>
            Installed: <b className="text-stone-200">{mod.installed_version ?? "not installed"}</b>
          </span>
          <span>
            Latest: <b className="text-stone-200">{mod.latest_version ?? "unknown"}</b>
          </span>
          <span>{mod.is_local ? "Local cache" : "Not in local cache"}</span>
          <span>{formatSize(mod.size)}</span>
          {mod.latest_game_version && <span>Game: {mod.latest_game_version}</span>}
        </div>
        {mod.pinned_reason && (
          <p className="text-[11px] text-amber-300">
            Pin reason: {mod.pinned_reason}
            {mod.pinned_at_build ? ` (engine build ${mod.pinned_at_build})` : ""}
          </p>
        )}
        {mod.tags?.length ? (
          <div className="flex flex-wrap gap-1">
            {mod.tags.slice(0, 6).map((tag, index) => (
              <Badge key={`${String(tag)}-${index}`}>{String(tag)}</Badge>
            ))}
          </div>
        ) : null}
      </div>
      <div className="flex flex-wrap content-start gap-2 lg:justify-end">
        <Button size="sm" variant="outline" onClick={onDownload} disabled={busy}>
          Re-download
        </Button>
        <Button size="sm" variant="ghost" onClick={onVerify} disabled={busy || !mod.is_local}>
          Verify
        </Button>
        {mod.pinned_version ? (
          <Button size="sm" variant="outline" onClick={onUnpin} disabled={busy}>
            Unpin
          </Button>
        ) : (
          <Button
            size="sm"
            variant="outline"
            onClick={onPin}
            disabled={busy || (!mod.latest_version && !mod.installed_version)}
          >
            Pin version
          </Button>
        )}
      </div>
    </article>
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