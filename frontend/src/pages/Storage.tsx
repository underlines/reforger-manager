import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { PageHeading } from "../components/PageHeading";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Dialog } from "../components/ui";
import { api, apiVoid } from "../lib/api";

type StorageMod = { guid: string; name: string | null; bytes: number };
type StorageKept = StorageMod & { required_by: string[] };
type StorageEntry = { guid: string; name: string | null };
type StorageReport = {
  mods_path: string;
  free_bytes: number;
  total_bytes: number;
  per_mod: StorageMod[];
  orphans: StorageMod[];
  kept_as_dependency: StorageKept[];
  unreferenced_entries: StorageEntry[];
};

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

export function StoragePage() {
  const queryClient = useQueryClient();
  const [removeTarget, setRemoveTarget] = useState<StorageMod | null>(null);
  const [libraryTarget, setLibraryTarget] = useState<StorageEntry | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const storageQuery = useQuery({
    queryKey: ["storage"],
    queryFn: () => api<StorageReport>("/api/storage"),
  });

  const removeMutation = useMutation({
    mutationFn: (mod: StorageMod) => apiVoid(`/api/mods/${mod.guid}/local`, { method: "DELETE" }),
    onSuccess: (_result, mod) => {
      setNotice(`${mod.name ?? mod.guid} removed from disk; the library row is kept.`);
      setRemoveTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["storage"] });
      void queryClient.invalidateQueries({ queryKey: ["mods"] });
    },
    onError: (error) => {
      setNotice(errorMessage(error));
      setRemoveTarget(null);
    },
  });

  const removeLibraryMutation = useMutation({
    mutationFn: (mod: StorageEntry) => apiVoid(`/api/mods/${mod.guid}`, { method: "DELETE" }),
    onSuccess: (_result, mod) => {
      setNotice(`${mod.name ?? mod.guid} removed from the library.`);
      setLibraryTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["storage"] });
      void queryClient.invalidateQueries({ queryKey: ["mods"] });
    },
    onError: (error) => {
      setNotice(errorMessage(error));
      setLibraryTarget(null);
    },
  });

  const confirmRemove = (mod: StorageMod) => removeMutation.mutate(mod);
  const data = storageQuery.data;

  return (
    <>
      <PageHeading
        title="Storage"
        detail="On-disk addon cache, free space, and orphaned mods that can be safely removed."
      />

      {notice && (
        <p className="mb-4 text-xs text-stone-300" role="status">
          {notice}
        </p>
      )}

      {storageQuery.isLoading && <p className="text-sm text-stone-400">Reading storage usage...</p>}
      {storageQuery.isError && (
        <div className="space-y-3">
          <p className="error">Could not load storage: {errorMessage(storageQuery.error)}</p>
          <Button size="sm" variant="outline" onClick={() => void storageQuery.refetch()}>
            Retry
          </Button>
        </div>
      )}

      {data && (
        <>
          <Card className="mb-4">
            <CardHeader>
              <CardTitle>Addon Cache Free Space</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <FreeSpaceBar freeBytes={data.free_bytes} totalBytes={data.total_bytes} />
              <p className="text-[11px] text-stone-400">
                Mods directory: <span className="font-mono text-stone-300">{data.mods_path}</span>
              </p>
            </CardContent>
          </Card>

          <Card className="mb-4">
            <CardHeader>
              <CardTitle>Per-Mod Sizes {data.per_mod.length ? `(${data.per_mod.length})` : ""}</CardTitle>
            </CardHeader>
            <CardContent>
              {data.per_mod.length ? (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs text-stone-300">
                    <thead>
                      <tr className="border-b border-stone-700 text-left text-[10px] uppercase tracking-widest text-stone-500">
                        <th className="py-2 pr-4 font-semibold">Mod</th>
                        <th className="py-2 pr-4 font-semibold">Size</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-stone-800">
                      {data.per_mod.map((mod) => (
                        <tr key={mod.guid}>
                          <td className="py-2 pr-4">
                            <span className="text-stone-200">{mod.name ?? mod.guid}</span>
                            <span className="ml-2 font-mono text-[10px] text-stone-500">{mod.guid}</span>
                          </td>
                          <td className="py-2 pr-4 font-mono">{formatSize(mod.bytes)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-xs text-stone-400">No mods are present in the local addon cache.</p>
              )}
            </CardContent>
          </Card>

          <Card className="mb-4">
            <CardHeader>
              <CardTitle>
                Orphans {data.orphans.length ? `(${data.orphans.length})` : ""}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {data.orphans.length ? (
                <ul className="divide-y divide-stone-800">
                  {data.orphans.map((mod) => (
                    <li
                      key={mod.guid}
                      className="grid gap-3 py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center"
                    >
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold uppercase tracking-wider text-stone-100">
                            {mod.name ?? mod.guid}
                          </span>
                          <Badge tone="warn">{formatSize(mod.bytes)}</Badge>
                        </div>
                        <p className="mt-0.5 font-mono text-[10px] text-stone-500">{mod.guid}</p>
                        <p className="text-[11px] text-stone-400">
                          Not referenced by any server definition, modpack, or resolved dependency.
                        </p>
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={removeMutation.isPending}
                        onClick={() => setRemoveTarget(mod)}
                      >
                        Remove from disk
                      </Button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-stone-400">No unreferenced mods — nothing to clean up.</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>
                Kept As Dependencies {data.kept_as_dependency.length ? `(${data.kept_as_dependency.length})` : ""}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {data.kept_as_dependency.length ? (
                <ul className="divide-y divide-stone-800">
                  {data.kept_as_dependency.map((mod) => (
                    <li key={mod.guid} className="py-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold uppercase tracking-wider text-stone-100">
                          {mod.name ?? mod.guid}
                        </span>
                        <Badge tone="neutral">{formatSize(mod.bytes)}</Badge>
                        <Badge tone="good">dependency</Badge>
                      </div>
                      <p className="mt-0.5 font-mono text-[10px] text-stone-500">{mod.guid}</p>
                      <p className="mt-1 text-[11px] leading-5 text-stone-400">
                        Required by:{" "}
                        {mod.required_by.map((guid) => (
                          <span key={guid} className="font-mono text-amber-300/90">
                            {guid}
                          </span>
                        ))}
                        <span className="text-stone-500">
                          {" "}
                          — kept on disk because deleting it would break a referenced mod.
                        </span>
                      </p>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-stone-400">
                  No dependency-only mods are being kept alive by the resolved dependency graph.
                </p>
              )}
            </CardContent>
          </Card>

          <Card className="mt-4">
            <CardHeader>
              <CardTitle>
                Unreferenced Library Entries{" "}
                {data.unreferenced_entries.length ? `(${data.unreferenced_entries.length})` : ""}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {data.unreferenced_entries.length ? (
                <ul className="divide-y divide-stone-800">
                  {data.unreferenced_entries.map((mod) => (
                    <li
                      key={mod.guid}
                      className="grid gap-3 py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center"
                    >
                      <div className="min-w-0">
                        <span className="text-sm font-semibold uppercase tracking-wider text-stone-100">
                          {mod.name ?? mod.guid}
                        </span>
                        <p className="mt-0.5 font-mono text-[10px] text-stone-500">{mod.guid}</p>
                        <p className="text-[11px] text-stone-400">
                          No files on disk and not referenced by any server, modpack, or dependency —
                          a stale catalogue row that cannot be cleaned up by a re-download.
                        </p>
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={removeLibraryMutation.isPending}
                        onClick={() => setLibraryTarget(mod)}
                      >
                        Remove from library
                      </Button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-stone-400">
                  No stale library rows — every catalogue entry is either on disk or in use.
                </p>
              )}
            </CardContent>
          </Card>
        </>
      )}

      <Dialog
        open={removeTarget !== null}
        title={`Remove ${removeTarget?.name ?? removeTarget?.guid ?? "mod"} from disk?`}
        onClose={() => !removeMutation.isPending && setRemoveTarget(null)}
      >
        <div className="space-y-4">
          <p className="text-xs leading-5 text-stone-400">
            This deletes the addon files for <b className="text-stone-200">{removeTarget?.guid}</b> from the
            local cache. The library row is kept, so the mod can be re-downloaded later. Deletion is refused
            while any server is running.
          </p>
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setRemoveTarget(null)}
              disabled={removeMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={removeMutation.isPending}
              onClick={() => removeTarget && confirmRemove(removeTarget)}
            >
              {removeMutation.isPending ? "Removing..." : "Remove files"}
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
            This deletes the library row for <b className="text-stone-200">{libraryTarget?.guid}</b>{" "}
            entirely — versions cache, dependency records, and scenario list. Nothing is on disk to
            remove. Add it again by Workshop URL/ID if you need it back. The delete is refused if the
            mod turns out to still be referenced.
          </p>
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