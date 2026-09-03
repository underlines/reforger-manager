import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { Empty } from "../Empty";
import { Badge, Button, Dialog, Input } from "../ui";
import {
  api,
  apiBlob,
  apiUpload,
  apiVoid,
  saveBlob,
  type ConfigResponse,
  type DetailServer,
  type DirListing,
  type FileContent,
  type FileEntry,
} from "../../lib/api";

const errText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

const joinPath = (base: string, seg: string) => (base ? `${base}/${seg}` : seg);
const parentOf = (rel: string) => {
  const index = rel.lastIndexOf("/");
  return index === -1 ? "" : rel.slice(0, index);
};

const fmtBytes = (bytes: number) => {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unit = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  const value = bytes / 1024 ** unit;
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
};

export function FilesPanel({ id, server }: { id: string; server: DetailServer }) {
  const queryClient = useQueryClient();
  const [path, setPath] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [editing, setEditing] = useState<FileContent | null>(null);
  const [text, setText] = useState("");
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [folderName, setFolderName] = useState("");
  const [renaming, setRenaming] = useState<FileEntry | null>(null);
  const [renameName, setRenameName] = useState("");
  const [confirmDeleteRel, setConfirmDeleteRel] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const segments = path.split("/").filter(Boolean);

  const files = useQuery({
    queryKey: ["server-files", id, path],
    queryFn: () => api<DirListing>(`/api/servers/${id}/files?path=${encodeURIComponent(path)}`),
  });

  const config = useQuery({
    queryKey: ["server-config", id],
    queryFn: () => api<ConfigResponse>(`/api/servers/${id}/config`),
  });

  const entries = files.data?.entries ?? [];

  const jsonErr = editing?.is_json
    ? (() => {
        try {
          JSON.parse(text);
          return null;
        } catch (e) {
          return (e as Error).message;
        }
      })()
    : null;

  const openFile = async (entry: FileEntry) => {
    try {
      const res = await api<FileContent>(
        `/api/servers/${id}/files/content?path=${encodeURIComponent(entry.rel_path)}`,
      );
      if (!res.editable) {
        setActionError(null);
        setNote(`${entry.name} is too large or not text — download it instead.`);
        return;
      }
      setNote(null);
      setActionError(null);
      setEditing(res);
      setText(res.content ?? "");
      setSaveErr(null);
    } catch (e) {
      setNote(null);
      setActionError(errText(e, "Open failed"));
    }
  };

  const downloadFile = async (entry: FileEntry) => {
    try {
      const blob = await apiBlob(
        `/api/servers/${id}/files/download?path=${encodeURIComponent(entry.rel_path)}`,
      );
      saveBlob(blob, entry.name);
    } catch (e) {
      setActionError(errText(e, "Download failed"));
    }
  };

  const saveFile = async () => {
    if (!editing) return;
    try {
      setSaving(true);
      setSaveErr(null);
      await api(`/api/servers/${id}/files/content?path=${encodeURIComponent(editing.path)}`, {
        method: "PUT",
        body: JSON.stringify({ content: text }),
      });
      setEditing(null);
      queryClient.invalidateQueries({ queryKey: ["server-files", id] });
    } catch (e) {
      setSaveErr(errText(e, "Save failed"));
    } finally {
      setSaving(false);
    }
  };

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["server-files", id] });

  useEffect(() => {
    if (!confirmDeleteRel) return;
    const timer = setTimeout(() => setConfirmDeleteRel(null), 4000);
    return () => clearTimeout(timer);
  }, [confirmDeleteRel]);

  const createFolder = async () => {
    const name = folderName.trim();
    if (!name) return;
    try {
      await api(`/api/servers/${id}/files/mkdir`, {
        method: "POST",
        body: JSON.stringify({ path: joinPath(path, name) }),
      });
      setNewFolderOpen(false);
      setFolderName("");
      invalidate();
    } catch (err) {
      setNewFolderOpen(false);
      setActionError(errText(err, "Could not create folder"));
    }
  };

  const onUploadPicked = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (!files.length) return;
    try {
      await apiUpload(
        `/api/servers/${id}/files/upload?path=${encodeURIComponent(path)}`,
        files,
      );
      invalidate();
    } catch (err) {
      setActionError(errText(err, "Upload failed"));
    } finally {
      e.target.value = "";
    }
  };

  const submitRename = async () => {
    if (!renaming) return;
    const newName = renameName.trim();
    if (!newName) return;
    const entry = renaming;
    try {
      await api(`/api/servers/${id}/files/rename`, {
        method: "POST",
        body: JSON.stringify({
          path: entry.rel_path,
          new_path: joinPath(parentOf(entry.rel_path), newName),
        }),
      });
      setRenaming(null);
      invalidate();
    } catch (err) {
      setRenaming(null);
      setActionError(errText(err, "Rename failed"));
    }
  };

  const deleteEntry = async (entry: FileEntry) => {
    try {
      await apiVoid(
        `/api/servers/${id}/files?path=${encodeURIComponent(entry.rel_path)}`,
        { method: "DELETE" },
      );
      setConfirmDeleteRel(null);
      invalidate();
    } catch (err) {
      setConfirmDeleteRel(null);
      setActionError(errText(err, "Delete failed"));
    }
  };

  return (
    <div className="grid gap-6">
      <details className="border border-stone-800 bg-stone-950/60">
        <summary className="cursor-pointer select-none px-3 py-2 font-display text-xs font-bold uppercase tracking-widest text-stone-300 hover:text-stone-100">
          Generated config.json (read-only)
        </summary>
        <div className="border-t border-stone-800 p-3">
          {config.isLoading ? (
            <p className="text-xs text-stone-400">Loading generated config...</p>
          ) : config.isError ? (
            <p className="error">Generated config could not be loaded.</p>
          ) : (
            config.data && (
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap font-mono text-xs text-stone-300">
                {JSON.stringify(config.data.config, null, 2)}
              </pre>
            )
          )}
        </div>
      </details>

      {server.is_running && (
        <div className="rounded-sm border border-amber-700 bg-amber-950/50 px-3 py-2 text-xs text-amber-300">
          This server is running &mdash; stop it to add, edit, rename or delete profile files.
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1 text-xs text-stone-500" aria-label="Path">
        {path === "" ? (
          <span className="font-mono text-[11px] text-stone-100">profile/</span>
        ) : (
          <button
            type="button"
            onClick={() => setPath("")}
            className="rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-stone-300 hover:bg-stone-800 hover:text-stone-100"
          >
            profile/
          </button>
        )}
        {segments.map((segment, index) => {
          const target = segments.slice(0, index + 1).join("/");
          const active = path === target;
          return (
            <span key={target} className="flex items-center gap-1">
              <span className="text-stone-600">/</span>
              {active ? (
                <span className="font-mono text-[11px] text-stone-100">{segment}</span>
              ) : (
                <button
                  type="button"
                  onClick={() => setPath(target)}
                  className="rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-stone-300 hover:bg-stone-800 hover:text-stone-100"
                >
                  {segment}
                </button>
              )}
            </span>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={server.is_running}
          onClick={() => {
            setFolderName("");
            setNewFolderOpen(true);
          }}
        >
          New Folder
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => void onUploadPicked(e)}
        />
        <Button
          size="sm"
          variant="outline"
          disabled={server.is_running}
          onClick={() => fileInputRef.current?.click()}
        >
          Upload
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={async () => {
            try {
              const blob = await apiBlob(`/api/servers/${id}/files/archive`);
              saveBlob(blob, `server-${id}-profile.zip`);
            } catch (e) {
              setActionError(errText(e, "Download failed"));
            }
          }}
        >
          Download all as .zip
        </Button>
      </div>

      {actionError && <p className="error">{actionError}</p>}
      {note && <p className="text-xs text-amber-300">{note}</p>}

      {files.isLoading ? (
        <p className="text-xs text-stone-400">Loading files...</p>
      ) : files.isError ? (
        <p className="error">{errText(files.error, "Failed to load files")}</p>
      ) : entries.length === 0 ? (
        path === "" ? (
          <Empty label="No profile directory yet — start the server once and its files will appear here." />
        ) : (
          <Empty label="This folder is empty." />
        )
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-stone-800 text-left text-xs uppercase tracking-wider text-stone-400">
              <th className="py-2 pr-3">Name</th>
              <th className="py-2 pr-3">Size</th>
              <th className="py-2 pr-3">Modified</th>
              <th className="py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry: FileEntry) => (
              <tr key={entry.rel_path} className="border-b border-stone-800">
                <td className="py-2 pr-3">
                  <span className="inline-flex items-center gap-2">
                    <Badge tone={entry.type === "symlink" ? "warn" : "neutral"}>
                      {entry.type === "dir" ? "DIR" : entry.type === "symlink" ? "LINK" : "FILE"}
                    </Badge>
                    {entry.type === "dir" ? (
                      <button
                        type="button"
                        onClick={() => setPath(entry.rel_path)}
                        className="rounded-sm font-medium text-stone-100 hover:bg-stone-800 hover:text-amber-300"
                      >
                        {entry.name}
                      </button>
                    ) : (
                      <span className="font-medium text-stone-300">{entry.name}</span>
                    )}
                  </span>
                </td>
                <td className="py-2 pr-3 font-mono text-xs text-stone-400">
                  {fmtBytes(entry.size)}
                </td>
                <td className="py-2 pr-3 text-xs text-stone-400">
                  {entry.mtime ? new Date(entry.mtime).toLocaleString() : "—"}
                </td>
                <td className="py-2 text-right">
                  {entry.type !== "symlink" && (
                    <span className="inline-flex items-center justify-end gap-1">
                      {entry.type === "file" && entry.is_text && (
                        <Button size="sm" variant="ghost" onClick={() => void openFile(entry)}>
                          Open
                        </Button>
                      )}
                      {entry.type === "file" && (
                        <Button size="sm" variant="ghost" onClick={() => void downloadFile(entry)}>
                          Download
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={server.is_running}
                        onClick={() => {
                          setRenaming(entry);
                          setRenameName(entry.name);
                        }}
                      >
                        Rename
                      </Button>
                      {confirmDeleteRel === entry.rel_path ? (
                        <>
                          {entry.type === "dir" && (
                            <span className="text-[11px] text-red-400">
                              Delete {entry.name}/ and everything in it?
                            </span>
                          )}
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={server.is_running}
                            title={
                              entry.type === "dir"
                                ? `Delete ${entry.name}/ and everything in it?`
                                : `Delete ${entry.name}?`
                            }
                            onClick={() => void deleteEntry(entry)}
                          >
                            Confirm
                          </Button>
                        </>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={server.is_running}
                          onClick={() => setConfirmDeleteRel(entry.rel_path)}
                        >
                          Delete
                        </Button>
                      )}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Dialog open={newFolderOpen} title="New folder" onClose={() => setNewFolderOpen(false)}>
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void createFolder();
          }}
        >
          <Input
            autoFocus
            placeholder="folder name"
            value={folderName}
            onChange={(e) => setFolderName(e.target.value)}
          />
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setNewFolderOpen(false)}
            >
              Cancel
            </Button>
            <Button type="submit" variant="default" size="sm" disabled={!folderName.trim()}>
              Create
            </Button>
          </div>
        </form>
      </Dialog>

      <Dialog
        open={renaming !== null}
        title={renaming ? `Rename ${renaming.name}` : ""}
        onClose={() => setRenaming(null)}
      >
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submitRename();
          }}
        >
          <Input
            autoFocus
            value={renameName}
            onChange={(e) => setRenameName(e.target.value)}
          />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setRenaming(null)}>
              Cancel
            </Button>
            <Button type="submit" variant="default" size="sm" disabled={!renameName.trim()}>
              Rename
            </Button>
          </div>
        </form>
      </Dialog>

      <Dialog open={editing !== null} title={editing?.path ?? ""} onClose={() => setEditing(null)}>
        <div className="grid gap-3">
          <textarea
            className="min-h-[6rem] w-full rounded-sm border border-stone-600 bg-stone-950 p-2 font-mono text-xs text-stone-100 outline-none focus:border-amber-400"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={20}
          />
          {jsonErr && <span className="error">{jsonErr}</span>}
          {saveErr && <span className="error">{saveErr}</span>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button
              variant="default"
              size="sm"
              disabled={jsonErr !== null || saving}
              onClick={() => void saveFile()}
            >
              {saving ? "Saving..." : "Save"}
            </Button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
