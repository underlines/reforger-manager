import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { PageHeading } from "../components/PageHeading";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Dialog, Input } from "../components/ui";
import { api, apiVoid, type Server } from "../lib/api";

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
  has_update: boolean;
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

const viaTone = (via: string) => (via === "api" ? "good" : via === "gproj" ? "neutral" : "warn");
const stateTone = (state: string) => (state === "ok" ? "good" : state === "not_found" ? "bad" : "warn");

export function ModDetailPage() {
  const { guid = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [pinOpen, setPinOpen] = useState(false);
  const [pinVersion, setPinVersion] = useState("");
  const [pinReason, setPinReason] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const invalidateMod = () => queryClient.invalidateQueries({ queryKey: ["mod", guid] });

  const detailQuery = useQuery({
    queryKey: ["mod", guid],
    queryFn: () => api<ModDetail>(`/api/mods/${guid}`),
    enabled: guid.length > 0,
  });

  const serversQuery = useQuery({
    queryKey: ["servers"],
    queryFn: () => api<Server[]>("/api/servers"),
  });

  const serverIdByName = useMemo(() => {
    const map = new Map<string, number>();
    for (const server of serversQuery.data ?? []) map.set(server.name, server.id);
    return map;
  }, [serversQuery.data]);

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
      setDeleteOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["mods"] });
      void queryClient.invalidateQueries({ queryKey: ["storage"] });
      navigate("/mods");
    },
    onError: (error) => {
      setDeleteOpen(false);
      setNotice(errorMessage(error));
    },
  });

  const openPinDialog = () => {
    const mod = detailQuery.data;
    setPinVersion(mod?.latest_version ?? mod?.installed_version ?? "");
    setPinReason(mod?.pinned_reason ?? "");
    setPinOpen(true);
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
  const busy =
    pinMutation.isPending ||
    unpinMutation.isPending ||
    verifyMutation.isPending ||
    downloadMutation.isPending ||
    deleteMutation.isPending;

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
            <Button variant="outline" onClick={() => setDeleteOpen(true)} disabled={busy}>
              Remove from library
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
              {detail.is_local && <Badge tone="good">Local</Badge>}
              {!detail.is_local && <Badge tone="neutral">Not local</Badge>}
              {detail.has_update && <Badge tone="warn">Update available</Badge>}
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
            {detail.versions.length ? (
              <table className="w-full text-xs text-stone-300">
                <thead>
                  <tr className="border-b border-stone-700 text-left text-[10px] uppercase tracking-widest text-stone-500">
                    <th className="py-2 pr-4 font-semibold">Version</th>
                    <th className="py-2 pr-4 font-semibold">Game version</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-stone-800">
                  {detail.versions.map((version, index) => (
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
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Dependency Tree</CardTitle>
          </CardHeader>
          <CardContent>
            {detail.dependency_tree?.nodes.length ? (
              <ul className="divide-y divide-stone-800">
                {[...detail.dependency_tree.nodes]
                  .sort((a, b) => a.depth - b.depth)
                  .map((node) => (
                    <li
                      key={node.guid}
                      className="flex flex-wrap items-center gap-2 py-2"
                      style={{ paddingLeft: `${Math.min(node.depth, 8) * 1.25}rem` }}
                    >
                      <span className="min-w-0 truncate font-mono text-stone-300">{node.name ?? node.guid}</span>
                      <Badge tone={viaTone(node.via)}>via {node.via}</Badge>
                      <Badge tone={stateTone(node.state)}>{node.state}</Badge>
                    </li>
                  ))}
              </ul>
            ) : (
              <p className="text-xs text-stone-400">No dependency information available.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Required By</CardTitle>
          </CardHeader>
          <CardContent>
            {detail.required_by.length ? (
              <>
                <p className="mb-2 text-xs text-stone-400">
                  These library mods declare this one as a dependency. Removing it from the library
                  or disk is refused while any of them is assigned to a server or in a modpack.
                </p>
                <ul className="divide-y divide-stone-800">
                  {detail.required_by.map((ref) => (
                    <li key={ref.guid} className="py-2">
                      <Link
                        to={`/mods/${ref.guid}`}
                        className="text-sm font-semibold uppercase tracking-wider text-amber-400 underline-offset-4 hover:underline"
                      >
                        {ref.name ?? ref.guid}
                      </Link>
                    </li>
                  ))}
                </ul>
              </>
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
                  return (
                    <li key={name} className="py-2">
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
        title={`Remove ${detail.name ?? detail.guid} from the library?`}
        onClose={() => !deleteMutation.isPending && setDeleteOpen(false)}
      >
        <div className="space-y-4">
          <p className="text-xs leading-5 text-stone-400">
            Deletes the library row for <b className="text-stone-200">{detail.guid}</b> — its version
            cache, dependency records, and scenarios.
            {detail.is_local
              ? " The on-disk addon files are deleted too. This is refused while a server is running."
              : " Nothing is on disk to remove."}{" "}
            The delete is refused if the mod is still referenced by a server definition, modpack, or
            resolved dependency. Add it again by Workshop URL/ID to restore it.
          </p>
          {detail.required_by.length ? (
            <p className="text-[11px] text-amber-300">
              {detail.required_by.map((ref) => ref.name ?? ref.guid).join(", ")}{" "}
              {detail.required_by.length === 1 ? "depends" : "depend"} on this mod — the delete will be
              refused while {detail.required_by.length === 1 ? "it is" : "any is"} assigned or packed.
            </p>
          ) : null}
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
              {deleteMutation.isPending ? "Removing..." : "Remove entry"}
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