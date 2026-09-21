import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { PageHeading } from "../components/PageHeading";
import { LocalStateBadge } from "../components/mods/LocalStateBadge";
import { ModTree } from "../components/mods/ModTree";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Dialog, Input } from "../components/ui";
import { api, apiVoid, type Server } from "../lib/api";
import { buildFlat, buildNested, buildReverse, useModGraph, type ModGraph } from "../lib/modGraph";

const EMPTY_GRAPH: ModGraph = { nodes: [], edges: [] };

type ModVersion = {
  version?: string | null;
  game_version?: string | null;
  [key: string]: unknown;
};

type DepNode = {
  guid: string;
  name: string | null;
  via: string;
  state: string;
  depth: number;
};

type DepTree = {
  roots: string[];
  nodes: DepNode[];
  edges: Array<{ from: string; to: string }>;
};

// Same shape as the live reference check Mods.tsx queries. detail.used_by /
// detail.required_by below are page-load-time and miss modpacks, so they are
// not used for the delete-dialog warnings — this is.
type ModReferences = {
  servers: string[];
  modpacks: string[];
  required_by: { guid: string; name: string | null }[];
};

type ModDetail = {
  guid: string;
  name: string | null;
  summary: string | null;
  installed_version: string | null;
  latest_version: string | null;
  latest_game_version: string | null;
  size: number | null;
  thumbnail: string | null;
  tags: unknown[] | null;
  last_checked: string | null;
  is_unlisted: boolean;
  is_private: boolean;
  is_obsolete: boolean;
  is_local: boolean;
  api_state: string;
  api_checked_at: string | null;
  pinned_version: string | null;
  pinned_at_build: string | null;
  pinned_reason: string | null;
  pinned_at: string | null;
  stale_pin: boolean;
  created_at: string | null;
  updated_at: string | null;
  scenarios: unknown[];
  dependencies: unknown[];
  dependency_tree: DepTree | null;
  versions: ModVersion[];
  used_by: string[];
  required_by: { guid: string; name: string | null }[];
};

type EnqueuedJob = { job_id: number; kind: string };

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

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

const relativeAge = (iso: string): string => {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "just now";
  const days = Math.floor(ms / 86_400_000);
  if (days < 1) return "just now";
  if (days < 30) return `${days} day${days === 1 ? "" : "s"}`;
  const months = Math.floor(days / 30);
  return `${months} month${months === 1 ? "" : "s"}`;
};

export function ModDetailPage() {
  const { guid = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [pinOpen, setPinOpen] = useState(false);
  const [pinVersion, setPinVersion] = useState("");
  const [pinReason, setPinReason] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [diskDeleteOpen, setDiskDeleteOpen] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [requiresMode, setRequiresMode] = useState<"nested" | "flat">("nested");
  const [requiredByMode, setRequiredByMode] = useState<"nested" | "flat">("flat");

  const invalidateMod = () => queryClient.invalidateQueries({ queryKey: ["mod", guid] });

  const detailQuery = useQuery({
    queryKey: ["mod", guid],
    queryFn: () => api<ModDetail>(`/api/mods/${guid}`),
    enabled: guid.length > 0,
  });

  // Live reference check backing the delete dialogs' warning — only runs
  // while one of those dialogs is open.
  const referencesQuery = useQuery({
    queryKey: ["mod-references", guid],
    queryFn: () => api<ModReferences>(`/api/mods/${guid}/references`),
    enabled: guid.length > 0 && (deleteOpen || diskDeleteOpen),
  });

  // Version history hits the live Workshop API — runs on request only (button
  // click below), never on mount / focus / reconnect.
  const versionsQuery = useQuery({
    queryKey: ["mod-versions", guid],
    queryFn: () => api<ModVersion[]>(`/api/mods/${guid}/versions`),
    enabled: false,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const graphQuery = useModGraph();
  const graph = graphQuery.data ?? EMPTY_GRAPH;

  const serversQuery = useQuery({
    queryKey: ["servers"],
    queryFn: () => api<Server[]>("/api/servers"),
  });

  const serverIdByName = useMemo(() => {
    const map = new Map<string, number>();
    for (const server of serversQuery.data ?? []) map.set(server.name, server.id);
    return map;
  }, [serversQuery.data]);

  const serverByName = useMemo(() => {
    const map = new Map<string, Server>();
    for (const server of serversQuery.data ?? []) map.set(server.name, server);
    return map;
  }, [serversQuery.data]);

  const usedByTags = useMemo(() => {
    const tags = new Map<string, "direct" | "indirect">();
    const detailGuid = detailQuery.data?.guid;
    if (!detailGuid) return tags;
    for (const name of detailQuery.data?.used_by ?? []) {
      const server = serverByName.get(name);
      if (!server) continue;
      const explicitGuids = server.mods.map((mod) => mod.mod_guid);
      if (explicitGuids.includes(detailGuid)) {
        tags.set(name, "direct");
        continue;
      }
      if (buildFlat(explicitGuids, graph).has(detailGuid)) {
        tags.set(name, "indirect");
      }
    }
    return tags;
  }, [detailQuery.data?.guid, detailQuery.data?.used_by, serverByName, graph]);

  const pinMutation = useMutation({
    mutationFn: ({ version, reason }: { version: string; reason: string }) =>
      api<ModDetail>(`/api/mods/${guid}/pin`, {
        method: "POST",
        body: JSON.stringify({ version, reason: reason || null }),
      }),
    onSuccess: (mod) => {
      setNotice(`${mod.name ?? mod.guid} pinned to ${mod.pinned_version}.`);
      setPinOpen(false);
      void invalidateMod();
      void queryClient.invalidateQueries({ queryKey: ["mods"] });
    },
    onError: (error) => setNotice(errorMessage(error)),
  });

  const unpinMutation = useMutation({
    mutationFn: () => api<ModDetail>(`/api/mods/${guid}/pin`, { method: "DELETE" }),
    onSuccess: (mod) => {
      setNotice(`${mod.name ?? mod.guid} is no longer pinned.`);
      void invalidateMod();
      void queryClient.invalidateQueries({ queryKey: ["mods"] });
    },
    onError: (error) => setNotice(errorMessage(error)),
  });

  const verifyMutation = useMutation({
    mutationFn: () =>
      api<EnqueuedJob>("/api/mods/verify", {
        method: "POST",
        body: JSON.stringify({ guids: [guid] }),
      }),
    onSuccess: (job) => setNotice(`Verify queued as job #${job.job_id}.`),
    onError: (error) => setNotice(errorMessage(error)),
  });

  const downloadMutation = useMutation({
    mutationFn: () =>
      api<EnqueuedJob>(`/api/mods/${guid}/download`, {
        method: "POST",
        body: JSON.stringify({}),
      }),
    onSuccess: (job) => setNotice(`Re-download queued as job #${job.job_id}.`),
    onError: (error) => setNotice(errorMessage(error)),
  });

  const deleteMutation = useMutation({
    mutationFn: () => apiVoid(`/api/mods/${guid}`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["mods"] });
      void queryClient.invalidateQueries({ queryKey: ["storage"] });
      navigate("/mods");
    },
    onError: (error) => {
      // Keep the dialog open on failure and show the reason inline — same
      // pattern as Mods.tsx's deleteError, instead of closing the dialog.
      const message = errorMessage(error);
      setDeleteError(message);
      setNotice(message);
    },
  });

  const removeLocalMutation = useMutation({
    mutationFn: () => apiVoid(`/api/mods/${guid}/local`, { method: "DELETE" }),
    onSuccess: () => {
      setNotice(`${detailQuery.data?.name ?? guid}: downloaded files deleted; the library entry is kept.`);
      setDiskDeleteOpen(false);
      setDeleteError(null);
      void invalidateMod();
      void queryClient.invalidateQueries({ queryKey: ["mods"] });
      void queryClient.invalidateQueries({ queryKey: ["storage"] });
    },
    onError: (error) => {
      const message = errorMessage(error);
      setDeleteError(message);
      setNotice(message);
    },
  });

  const openPinDialog = () => {
    const mod = detailQuery.data;
    setPinVersion(mod?.installed_version ?? "");
    setPinReason(mod?.pinned_reason ?? "");
    setPinOpen(true);
  };

  const openDeleteDialog = () => {
    setDeleteError(null);
    setDiskDeleteOpen(false);
    setDeleteOpen(true);
  };

  const openDiskDeleteDialog = () => {
    setDeleteError(null);
    setDeleteOpen(false);
    setDiskDeleteOpen(true);
  };

  const renderReferences = (kind: "disk" | "full") => {
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
    const blocksDiskDelete = !data.servers.length && !data.modpacks.length && data.required_by.length > 0;
    return (
      <div className="space-y-2 border border-red-800 bg-red-950/50 p-3 text-sm text-red-300">
        <Badge tone="bad">Still referenced</Badge>
        {data.servers.length ? (
          <p>
            Server definitions: <span className="font-semibold">{data.servers.join(", ")}</span>
          </p>
        ) : null}
        {data.modpacks.length ? (
          <p>
            Modpacks: <span className="font-semibold">{data.modpacks.join(", ")}</span>
          </p>
        ) : null}
        {data.required_by.length ? (
          <p>
            Required by:{" "}
            <span className="font-semibold">
              {data.required_by.map((ref) => ref.name ?? ref.guid).join(", ")}
            </span>
          </p>
        ) : null}
        <p className="text-red-300/80">
          {kind === "full"
            ? "The delete is refused while any of these hold a reference."
            : blocksDiskDelete
              ? "This still blocks deleting the downloaded files — it's kept alive as a dependency of another mod."
              : "None of these block deleting the downloaded files — only a dependency link would. The assignments above are kept, and this mod is refetched automatically the next time it's needed."}
        </p>
      </div>
    );
  };

  const submitPin = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!pinVersion.trim()) return;
    pinMutation.mutate({ version: pinVersion.trim(), reason: pinReason.trim() });
  };

  if (detailQuery.isLoading) {
    return <p className="text-sm text-stone-400">Loading mod detail...</p>;
  }
  if (detailQuery.isError || !detailQuery.data) {
    return (
      <div className="space-y-3">
        <p className="error">Could not load the mod: {errorMessage(detailQuery.error)}</p>
        <Button size="sm" variant="outline" onClick={() => void detailQuery.refetch()}>
          Retry
        </Button>
      </div>
    );
  }

  const detail = detailQuery.data;
  const checkedIso = detail.api_checked_at ?? detail.last_checked;
  const checkedMs = checkedIso ? Date.now() - new Date(checkedIso).getTime() : NaN;
  const checkedDays = Number.isFinite(checkedMs) ? Math.floor(checkedMs / 86_400_000) : null;
  const requiresNested = buildNested([detail.guid], graph);
  const requiresFlat = buildFlat([detail.guid], graph);
  const requiredByNested = buildReverse(detail.guid, graph, "nested");
  const requiredByFlat = buildReverse(detail.guid, graph, "flat");
  const busy =
    pinMutation.isPending ||
    unpinMutation.isPending ||
    verifyMutation.isPending ||
    downloadMutation.isPending ||
    deleteMutation.isPending ||
    removeLocalMutation.isPending;

  return (
    <>
      <PageHeading
        title={detail.name ?? detail.guid}
        detail={`${detail.guid} · Workshop availability, version history, dependencies, and usage.`}
        actions={
          <>
            <Link
              to="/mods"
              className="inline-flex h-9 items-center justify-center border border-stone-500 px-3 font-display text-sm font-bold uppercase tracking-wider text-stone-100 transition-colors hover:border-stone-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400"
            >
              Mod Library
            </Link>
            <Button variant="outline" onClick={() => downloadMutation.mutate()} disabled={busy}>
              {downloadMutation.isPending ? "Queueing..." : "Re-download"}
            </Button>
            <Button variant="outline" onClick={() => verifyMutation.mutate()} disabled={busy || !detail.is_local}>
              {verifyMutation.isPending ? "Verifying..." : "Verify"}
            </Button>
            {detail.pinned_version ? (
              <Button variant="outline" onClick={() => unpinMutation.mutate()} disabled={busy}>
                Unpin
              </Button>
            ) : (
              <Button
                onClick={openPinDialog}
                disabled={busy || (!detail.latest_version && !detail.installed_version)}
              >
                Pin version
              </Button>
            )}
            <Button variant="outline" onClick={openDiskDeleteDialog} disabled={busy || !detail.is_local}>
              Delete downloaded files
            </Button>
            <Button variant="outline" onClick={openDeleteDialog} disabled={busy}>
              Delete mod entirely
            </Button>
          </>
        }
      />

      {notice && (
        <p className="mb-4 text-xs text-stone-300" role="status">
          {notice}
        </p>
      )}

      <div className="mb-4 grid gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Summary</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <LocalStateBadge
                guid={detail.guid}
                isLocal={detail.is_local}
                onDone={() => void invalidateMod()}
              />
              {detail.stale_pin && <Badge tone="bad">Stale pin</Badge>}
              {detail.pinned_version && !detail.stale_pin && (
                <Badge tone="neutral">Pinned {detail.pinned_version}</Badge>
              )}
            </div>
            {detail.summary ? (
              <p className="text-sm leading-6 text-stone-200">{detail.summary}</p>
            ) : (
              <p className="text-xs text-stone-400">No summary provided by the Workshop.</p>
            )}
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-stone-400">
              <span className="font-mono text-stone-500">{detail.guid}</span>
              <span>
                Installed: <b className="text-stone-200">{detail.installed_version ?? "not installed"}</b>
              </span>
              <span>
                Latest: <b className="text-stone-200">{detail.latest_version ?? "unknown"}</b>
              </span>
              <span>Size: {formatSize(detail.size)}</span>
              {detail.latest_game_version && <span>Game: {detail.latest_game_version}</span>}
            </div>
            {checkedIso === null || checkedDays === null ? (
              <p className="text-xs text-amber-300">Never checked against the Workshop</p>
            ) : checkedDays >= 30 ? (
              <p className="text-xs text-amber-300">
                Checked {relativeAge(checkedIso)} ago — may be out of date
              </p>
            ) : (
              <p className="text-xs text-stone-400">Checked {relativeAge(checkedIso)} ago</p>
            )}
            {detail.pinned_reason && (
              <p className="text-[11px] text-amber-300">
                Pin reason: {detail.pinned_reason}
                {detail.pinned_at_build ? ` (engine build ${detail.pinned_at_build})` : ""}
              </p>
            )}
            {detail.tags?.length ? (
              <div className="flex flex-wrap gap-1">
                {detail.tags.map((tag, index) => (
                  <Badge key={`${String(tag)}-${index}`}>{String(tag)}</Badge>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Version History</CardTitle>
          </CardHeader>
          <CardContent>
            {versionsQuery.data ? (
              versionsQuery.data.length ? (
                <table className="w-full text-xs text-stone-300">
                  <thead>
                    <tr className="border-b border-stone-700 text-left text-[10px] uppercase tracking-widest text-stone-500">
                      <th className="py-2 pr-4 font-semibold">Version</th>
                      <th className="py-2 pr-4 font-semibold">Game version</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-stone-800">
                    {versionsQuery.data.map((version, index) => (
                      <tr key={`${String(version.version)}-${index}`}>
                        <td className="py-2 pr-4 font-mono text-stone-200">{version.version ?? "—"}</td>
                        <td className="py-2 pr-4">{version.game_version ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-xs leading-5 text-stone-400">
                  No version history available — the mod may be new, or the Workshop API was unreachable.
                </p>
              )
            ) : versionsQuery.isFetching ? (
              <p className="text-xs text-stone-400">Loading version history...</p>
            ) : (
              <div className="flex flex-wrap items-center gap-3">
                <Button size="sm" onClick={() => void versionsQuery.refetch()}>
                  Load version history
                </Button>
                {versionsQuery.isError && (
                  <p className="text-xs text-amber-300">
                    Failed to load version history: {errorMessage(versionsQuery.error)}
                  </p>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>This Mod Requires</CardTitle>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant={requiresMode === "nested" ? "default" : "outline"}
                onClick={() => setRequiresMode("nested")}
              >
                Nested
              </Button>
              <Button
                size="sm"
                variant={requiresMode === "flat" ? "default" : "outline"}
                onClick={() => setRequiresMode("flat")}
              >
                Flat
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {requiresMode === "nested" ? (
              requiresNested.some((root) => root.children.length) ? (
                <ModTree graph={graph} mode="nested" nested={requiresNested} linkTo={(g) => `/mods/${g}`} />
              ) : (
                <p className="text-xs text-stone-400">No dependency information available.</p>
              )
            ) : requiresFlat.size ? (
              <ModTree graph={graph} mode="flat" flat={requiresFlat} linkTo={(g) => `/mods/${g}`} />
            ) : (
              <p className="text-xs text-stone-400">No dependency information available.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Required By</CardTitle>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant={requiredByMode === "nested" ? "default" : "outline"}
                onClick={() => setRequiredByMode("nested")}
              >
                Nested
              </Button>
              <Button
                size="sm"
                variant={requiredByMode === "flat" ? "default" : "outline"}
                onClick={() => setRequiredByMode("flat")}
              >
                Flat
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <p className="mb-2 text-xs text-stone-400">
              These library mods declare this one as a dependency. Removing it from the library
              or disk is refused while any of them is assigned to a server or in a modpack.
            </p>
            {requiredByMode === "nested" ? (
              requiredByNested.some((root) => root.children.length) ? (
                <ModTree graph={graph} mode="nested" nested={requiredByNested} linkTo={(g) => `/mods/${g}`} />
              ) : (
                <p className="text-xs text-stone-400">No other mod depends on this one.</p>
              )
            ) : requiredByFlat.size ? (
              <ModTree graph={graph} mode="flat" flat={requiredByFlat} linkTo={(g) => `/mods/${g}`} />
            ) : (
              <p className="text-xs text-stone-400">No other mod depends on this one.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Used By</CardTitle>
          </CardHeader>
          <CardContent>
            {detail.used_by.length ? (
              <ul className="divide-y divide-stone-800">
                {detail.used_by.map((name) => {
                  const serverId = serverIdByName.get(name);
                  const tag = usedByTags.get(name);
                  return (
                    <li key={name} className="flex flex-wrap items-center gap-2 py-2">
                      {serverId !== undefined ? (
                        <Link
                          to={`/servers/${serverId}`}
                          className="text-sm font-semibold uppercase tracking-wider text-amber-400 underline-offset-4 hover:underline"
                        >
                          {name}
                        </Link>
                      ) : (
                        <span className="text-sm text-stone-200">{name}</span>
                      )}
                      {tag ? <Badge tone="neutral">{tag}</Badge> : null}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="text-xs text-stone-400">No server definitions reference this mod.</p>
            )}
          </CardContent>
        </Card>
      </div>

      <Dialog
        open={deleteOpen}
        title={`Delete ${detail.name ?? detail.guid} entirely?`}
        onClose={() => !deleteMutation.isPending && setDeleteOpen(false)}
      >
        <div className="space-y-4">
          {renderReferences("full")}
          <p className="text-xs leading-5 text-stone-400">
            Deletes the library entry for <b className="text-stone-200">{detail.guid}</b> entirely — its
            {detail.is_local ? " downloaded files," : ""} version cache, dependency records, and scenarios.
            The delete is refused if the mod is still referenced, directly or through a dependency, by any
            server definition or modpack. To use this mod again afterward, re-add it by Workshop URL or
            GUID.
          </p>
          {deleteError && <p className="error">{deleteError}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteOpen(false)}
              disabled={deleteMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => deleteMutation.mutate()}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending ? "Deleting..." : "Delete mod entirely"}
            </Button>
          </div>
        </div>
      </Dialog>

      <Dialog
        open={diskDeleteOpen}
        title={`Delete downloaded files for ${detail.name ?? detail.guid}?`}
        onClose={() => !removeLocalMutation.isPending && setDiskDeleteOpen(false)}
      >
        <div className="space-y-4">
          {renderReferences("disk")}
          <p className="text-xs leading-5 text-stone-400">
            Deletes only the cached addon files for <b className="text-stone-200">{detail.guid}</b> from
            local disk. The library entry and every reference to this mod — server assignments, modpacks,
            and dependency links — are kept untouched, and it will be refetched automatically the next time
            a server that needs it starts. Deletion is refused while any server is running.
          </p>
          {deleteError && <p className="error">{deleteError}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDiskDeleteOpen(false)}
              disabled={removeLocalMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => removeLocalMutation.mutate()}
              disabled={removeLocalMutation.isPending}
            >
              {removeLocalMutation.isPending ? "Deleting..." : "Delete downloaded files"}
            </Button>
          </div>
        </div>
      </Dialog>

      <Dialog
        open={pinOpen}
        title={`Pin ${detail.name ?? detail.guid}`}
        onClose={() => !pinMutation.isPending && setPinOpen(false)}
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
            <Button type="button" variant="ghost" onClick={() => setPinOpen(false)} disabled={pinMutation.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={pinMutation.isPending || !pinVersion.trim()}>
              {pinMutation.isPending ? "Pinning..." : "Pin version"}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}