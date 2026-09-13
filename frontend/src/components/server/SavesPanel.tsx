import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Empty } from "../Empty";
import { Badge, Button, Dialog, Input } from "../ui";
import {
  api,
  apiBlob,
  apiUpload,
  apiVoid,
  saveBlob,
  type DetailServer,
  type RestoreResult,
  type SavePointOut,
  type SavesListOut,
  type SelectionOut,
  type SnapshotOut,
} from "../../lib/api";

const errText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

const fmtBytes = (bytes: number | null | undefined) => {
  if (bytes === null || bytes === undefined || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unit = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  const value = bytes / 1024 ** unit;
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
};

const fmtDate = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "—");

const fmtPlaytime = (seconds: number | null | undefined) => {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
};

function findSaveByUuid(data: SavesListOut | undefined, uuid: string | null): SavePointOut | null {
  if (!data || !uuid) return null;
  for (const scenario of data.scenarios) {
    for (const playthrough of scenario.playthroughs) {
      const found = playthrough.save_points.find((sp) => sp.uuid === uuid);
      if (found) return found;
    }
  }
  return null;
}

function slotOccupied(data: SavesListOut | undefined, snap: SnapshotOut): boolean {
  if (!data || !snap.scenario_dir || !snap.playthrough_dir_name || !snap.save_point_dir_name) return false;
  const scenario = data.scenarios.find((s) => s.scenario_dir === snap.scenario_dir);
  const playthrough = scenario?.playthroughs.find((p) => p.dir_name === snap.playthrough_dir_name);
  return Boolean(playthrough?.save_points.some((sp) => sp.dir_name === snap.save_point_dir_name));
}

function modDriftBadge(sp: SavePointOut) {
  if (sp.mod_drift_unknown) {
    return (
      <Badge tone="warn" title="No mod manifest was recorded for this save point">
        mod set unknown
      </Badge>
    );
  }
  const drift = sp.mod_drift;
  if (drift && (drift.added.length > 0 || drift.removed.length > 0)) {
    const title = [
      drift.added.length ? `Added: ${drift.added.join(", ")}` : null,
      drift.removed.length ? `Removed: ${drift.removed.join(", ")}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    return (
      <Badge tone="warn" title={title}>
        {drift.added.length} added, {drift.removed.length} removed
      </Badge>
    );
  }
  return null;
}

export function SavesPanel({
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
  const running = server.is_running;

  const savesQuery = useQuery({
    queryKey: ["server-saves", id],
    queryFn: () => api<SavesListOut>(`/api/servers/${id}/saves`),
  });
  const data = savesQuery.data;

  const [actionError, setActionError] = useState<string | null>(null);
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["server-saves", id] });

  // click-to-confirm delete state, auto-clears like FilesPanel
  const [confirmDeleteSave, setConfirmDeleteSave] = useState<string | null>(null);
  const [confirmDeletePlaythrough, setConfirmDeletePlaythrough] = useState<string | null>(null);
  const [confirmDeleteSnapshot, setConfirmDeleteSnapshot] = useState<string | null>(null);
  useEffect(() => {
    if (!confirmDeleteSave && !confirmDeletePlaythrough && !confirmDeleteSnapshot) return;
    const timer = setTimeout(() => {
      setConfirmDeleteSave(null);
      setConfirmDeletePlaythrough(null);
      setConfirmDeleteSnapshot(null);
    }, 4000);
    return () => clearTimeout(timer);
  }, [confirmDeleteSave, confirmDeletePlaythrough, confirmDeleteSnapshot]);

  // arm dialog (only shown while running — direct arm otherwise)
  const [armDialogSave, setArmDialogSave] = useState<SavePointOut | null>(null);
  const [armSticky, setArmSticky] = useState(false);
  const [armBusy, setArmBusy] = useState(false);
  const [armErr, setArmErr] = useState<string | null>(null);

  // snapshot-from-save-point dialog
  const [snapshotDialogSave, setSnapshotDialogSave] = useState<SavePointOut | null>(null);
  const [snapshotLabel, setSnapshotLabel] = useState("");
  const [snapshotBusy, setSnapshotBusy] = useState(false);
  const [snapshotErr, setSnapshotErr] = useState<string | null>(null);

  // restore dialog
  const [restoreDialog, setRestoreDialog] = useState<SnapshotOut | null>(null);
  const [restoreArm, setRestoreArm] = useState(true);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [restoreErr, setRestoreErr] = useState<string | null>(null);

  // rename snapshot dialog
  const [renameSnapshot, setRenameSnapshot] = useState<SnapshotOut | null>(null);
  const [renameLabel, setRenameLabel] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameErr, setRenameErr] = useState<string | null>(null);

  // upload dialog
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadLabel, setUploadLabel] = useState("");
  const [uploadRestoreNow, setUploadRestoreNow] = useState(false);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadErr, setUploadErr] = useState<string | null>(null);

  const cancelSelection = async () => {
    setActionError(null);
    try {
      await api<SelectionOut>(`/api/servers/${id}/saves/selection`, { method: "DELETE" });
      invalidate();
    } catch (e) {
      setActionError(errText(e, "Could not cancel the pending selection"));
    }
  };

  const armDirect = async (sp: SavePointOut) => {
    if (!sp.uuid) return;
    setActionError(null);
    try {
      await api<SelectionOut>(`/api/servers/${id}/saves/${sp.uuid}/arm`, {
        method: "POST",
        body: JSON.stringify({ sticky: false }),
      });
      invalidate();
    } catch (e) {
      setActionError(errText(e, "Could not arm save point"));
    }
  };

  const openArmDialog = (sp: SavePointOut) => {
    setArmSticky(false);
    setArmErr(null);
    setArmDialogSave(sp);
  };

  const armForNextRestart = async () => {
    if (!armDialogSave?.uuid) return;
    setArmBusy(true);
    setArmErr(null);
    try {
      await api<SelectionOut>(`/api/servers/${id}/saves/${armDialogSave.uuid}/arm`, {
        method: "POST",
        body: JSON.stringify({ sticky: armSticky }),
      });
      setArmDialogSave(null);
      invalidate();
    } catch (e) {
      setArmErr(errText(e, "Could not arm save point"));
    } finally {
      setArmBusy(false);
    }
  };

  const restartNowAndLoad = async () => {
    if (!armDialogSave?.uuid) return;
    setArmBusy(true);
    setArmErr(null);
    try {
      await api<SelectionOut>(`/api/servers/${id}/saves/${armDialogSave.uuid}/arm`, {
        method: "POST",
        body: JSON.stringify({ sticky: armSticky }),
      });
    } catch (e) {
      setArmErr(errText(e, "Could not arm save point"));
      setArmBusy(false);
      return;
    }
    setArmDialogSave(null);
    setArmBusy(false);
    await run("restart-load", async () => {
      await api(`/api/servers/${id}/stop`, { method: "POST" });
      await api(`/api/servers/${id}/start`, { method: "POST" });
    });
    invalidate();
  };

  const downloadSave = async (sp: SavePointOut) => {
    if (!sp.uuid) return;
    setActionError(null);
    try {
      const blob = await apiBlob(`/api/servers/${id}/saves/${sp.uuid}/download`);
      saveBlob(blob, `${sp.dir_name}.tar.gz`);
    } catch (e) {
      setActionError(errText(e, "Download failed"));
    }
  };

  const deleteSave = async (sp: SavePointOut) => {
    if (!sp.uuid) return;
    try {
      await apiVoid(`/api/servers/${id}/saves/${sp.uuid}`, { method: "DELETE" });
      setConfirmDeleteSave(null);
      invalidate();
    } catch (e) {
      setConfirmDeleteSave(null);
      setActionError(errText(e, "Delete failed"));
    }
  };

  const deletePlaythrough = async (scenarioDir: string, playthroughNr: number) => {
    try {
      await apiVoid(`/api/servers/${id}/saves/playthroughs/${scenarioDir}/${playthroughNr}`, {
        method: "DELETE",
      });
      setConfirmDeletePlaythrough(null);
      invalidate();
    } catch (e) {
      setConfirmDeletePlaythrough(null);
      setActionError(errText(e, "Delete failed"));
    }
  };

  const submitSnapshot = async () => {
    if (!snapshotDialogSave?.uuid) return;
    const label = snapshotLabel.trim();
    if (!label) return;
    setSnapshotBusy(true);
    setSnapshotErr(null);
    try {
      await api<SnapshotOut>(`/api/servers/${id}/saves/${snapshotDialogSave.uuid}/snapshot`, {
        method: "POST",
        body: JSON.stringify({ label }),
      });
      setSnapshotDialogSave(null);
      invalidate();
    } catch (e) {
      setSnapshotErr(errText(e, "Snapshot failed"));
    } finally {
      setSnapshotBusy(false);
    }
  };

  const submitRestore = async () => {
    if (!restoreDialog) return;
    setRestoreBusy(true);
    setRestoreErr(null);
    try {
      await api<RestoreResult>(`/api/servers/${id}/saves/snapshots/${restoreDialog.snapshot_id}/restore`, {
        method: "POST",
        body: JSON.stringify({ arm: restoreArm }),
      });
      setRestoreDialog(null);
      invalidate();
    } catch (e) {
      setRestoreErr(errText(e, "Restore failed"));
    } finally {
      setRestoreBusy(false);
    }
  };

  const downloadSnapshot = async (snap: SnapshotOut) => {
    setActionError(null);
    try {
      const blob = await apiBlob(`/api/servers/${id}/saves/snapshots/${snap.snapshot_id}/download`);
      saveBlob(blob, `${snap.label || snap.snapshot_id}.tar.gz`);
    } catch (e) {
      setActionError(errText(e, "Download failed"));
    }
  };

  const submitRenameSnapshot = async () => {
    if (!renameSnapshot) return;
    const label = renameLabel.trim();
    if (!label) return;
    setRenameBusy(true);
    setRenameErr(null);
    try {
      await api<SnapshotOut>(`/api/servers/${id}/saves/snapshots/${renameSnapshot.snapshot_id}`, {
        method: "PATCH",
        body: JSON.stringify({ label }),
      });
      setRenameSnapshot(null);
      invalidate();
    } catch (e) {
      setRenameErr(errText(e, "Rename failed"));
    } finally {
      setRenameBusy(false);
    }
  };

  const deleteSnapshot = async (snap: SnapshotOut) => {
    try {
      await apiVoid(`/api/servers/${id}/saves/snapshots/${snap.snapshot_id}`, { method: "DELETE" });
      setConfirmDeleteSnapshot(null);
      invalidate();
    } catch (e) {
      setConfirmDeleteSnapshot(null);
      setActionError(errText(e, "Delete failed"));
    }
  };

  const submitUpload = async () => {
    if (!uploadFile) return;
    const label = uploadLabel.trim();
    if (!label) return;
    setUploadBusy(true);
    setUploadErr(null);
    try {
      await apiUpload<SnapshotOut>(`/api/servers/${id}/saves/snapshots/upload`, [uploadFile], {
        fieldName: "file",
        fields: {
          label,
          restore_now: String(uploadRestoreNow),
          arm: "true",
        },
      });
      setUploadOpen(false);
      setUploadFile(null);
      setUploadLabel("");
      setUploadRestoreNow(false);
      invalidate();
    } catch (e) {
      setUploadErr(errText(e, "Upload failed"));
    } finally {
      setUploadBusy(false);
    }
  };

  if (savesQuery.isLoading) return <p className="text-xs text-stone-400">Loading saves...</p>;
  if (savesQuery.isError || !data) {
    return <p className="error">{errText(savesQuery.error, "Failed to load saves")}</p>;
  }

  const selection = data.selection;
  const hasAnySavePoints = data.scenarios.some((s) => s.playthroughs.some((p) => p.save_points.length > 0));

  return (
    <div className="grid gap-6">
      {selection.mode !== "latest" && (
        <div
          className={
            selection.sticky
              ? "flex flex-wrap items-center justify-between gap-2 rounded-sm border border-amber-700 bg-amber-950/50 px-3 py-2 text-xs text-amber-300"
              : "flex flex-wrap items-center justify-between gap-2 rounded-sm border border-stone-700 bg-stone-900/60 px-3 py-2 text-xs text-stone-300"
          }
        >
          <span>
            {selection.mode === "pinned"
              ? (() => {
                  const sp = findSaveByUuid(data, selection.pinned_uuid);
                  return sp
                    ? `Next start: playthrough ${sp.playthrough_nr} · save point ${sp.save_point_nr} · ${fmtDate(sp.saved_at)}`
                    : "Next start: a pinned save point";
                })()
              : "Next start: a new playthrough"}
            {selection.sticky && " — Every restart will use this setting."}
          </span>
          <Button size="sm" variant="outline" onClick={() => void cancelSelection()}>
            Cancel
          </Button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs text-stone-400">
        <Badge>{server.auto_save_interval} min autosave</Badge>
        <Badge>{server.save_retention} retained</Badge>
        <Badge tone={server.load_session_save ? "good" : "neutral"}>
          load on boot: {server.load_session_save ? "yes" : "no"}
        </Badge>
        <span className="text-stone-500">Persistence settings live on the Config tab.</span>
      </div>

      {running && (
        <div className="rounded-sm border border-amber-700 bg-amber-950/50 px-3 py-2 text-xs text-amber-300">
          This server is running &mdash; stop it to snapshot, download, rename or delete saves. Arming a
          save point for the next restart still works.
        </div>
      )}

      {actionError && <p className="error">{actionError}</p>}

      <section className="grid gap-3">
        <h3 className="font-display text-sm font-bold uppercase tracking-widest text-stone-300">
          Save points
        </h3>
        {!hasAnySavePoints ? (
          <Empty label="This server hasn't written a save yet. Save points appear after the first autosave (every 10 minutes) or a clean stop." />
        ) : (
          data.scenarios.map((scenario) => (
            <div key={scenario.scenario_dir} className="grid gap-2">
              <div className="flex flex-wrap items-center gap-2 text-xs text-stone-400">
                <span className="font-mono">{scenario.scenario_dir}</span>
                {!scenario.matches_current_scenario && <Badge tone="warn">different scenario</Badge>}
              </div>
              {scenario.playthroughs.map((playthrough) => {
                const ptKey = `${scenario.scenario_dir}/${playthrough.playthrough_nr}`;
                return (
                  <div key={ptKey} className="border border-stone-800 bg-stone-950/40">
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-stone-800 px-3 py-2">
                      <span className="text-xs text-stone-300">
                        Playthrough {playthrough.playthrough_nr}
                        {playthrough.display_name ? ` — ${playthrough.display_name}` : ""}
                        {playthrough.started_at ? ` · started ${fmtDate(playthrough.started_at)}` : ""}
                      </span>
                      {confirmDeletePlaythrough === ptKey ? (
                        <span className="inline-flex items-center gap-2">
                          <span className="text-[11px] text-red-400">
                            Delete this playthrough and all its save points?
                          </span>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={running}
                            onClick={() =>
                              void deletePlaythrough(scenario.scenario_dir, playthrough.playthrough_nr)
                            }
                          >
                            Confirm
                          </Button>
                        </span>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={running}
                          onClick={() => setConfirmDeletePlaythrough(ptKey)}
                        >
                          Delete playthrough
                        </Button>
                      )}
                    </div>
                    <table className="w-full border-collapse text-sm">
                      <thead>
                        <tr className="border-b border-stone-800 text-left text-xs uppercase tracking-wider text-stone-400">
                          <th className="py-2 pl-3 pr-3">Save point</th>
                          <th className="py-2 pr-3">Saved at</th>
                          <th className="py-2 pr-3">Playtime</th>
                          <th className="py-2 pr-3">Version</th>
                          <th className="py-2 pr-3">Size</th>
                          <th className="py-2 pr-3">Flags</th>
                          <th className="py-2 pr-3 text-right">Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {playthrough.save_points.map((sp) => (
                          <tr key={sp.dir_name} className="border-b border-stone-800">
                            <td className="py-2 pl-3 pr-3 font-mono text-xs text-stone-300">
                              #{sp.save_point_nr}
                              {sp.display_name ? ` — ${sp.display_name}` : ""}
                              {!sp.readable && (
                                <Badge tone="bad" className="ml-2">
                                  unreadable
                                </Badge>
                              )}
                            </td>
                            <td className="py-2 pr-3 text-xs text-stone-400">{fmtDate(sp.saved_at)}</td>
                            <td className="py-2 pr-3 font-mono text-xs text-stone-400">
                              {fmtPlaytime(sp.playtime_seconds)}
                            </td>
                            <td className="py-2 pr-3 text-xs text-stone-400">{sp.game_version ?? "—"}</td>
                            <td className="py-2 pr-3 font-mono text-xs text-stone-400">
                              {fmtBytes(sp.size_bytes)}
                            </td>
                            <td className="py-2 pr-3">
                              <span className="flex flex-wrap gap-1">
                                {!sp.matches_current_scenario && <Badge tone="warn">different scenario</Badge>}
                                {sp.engine_drift && (
                                  <Badge tone="warn">made on {sp.game_version ?? "unknown version"}</Badge>
                                )}
                                {modDriftBadge(sp)}
                              </span>
                            </td>
                            <td className="py-2 pr-3 text-right">
                              <span className="inline-flex flex-wrap items-center justify-end gap-1">
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={!sp.uuid || !sp.readable}
                                  onClick={() =>
                                    running ? openArmDialog(sp) : void armDirect(sp)
                                  }
                                >
                                  Load next start
                                </Button>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={!sp.uuid || !sp.readable}
                                  onClick={() => {
                                    setSnapshotLabel("");
                                    setSnapshotErr(null);
                                    setSnapshotDialogSave(sp);
                                  }}
                                >
                                  Snapshot
                                </Button>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={!sp.uuid}
                                  onClick={() => void downloadSave(sp)}
                                >
                                  Download
                                </Button>
                                {confirmDeleteSave === sp.dir_name ? (
                                  <Button
                                    size="sm"
                                    variant="outline"
                                    disabled={running}
                                    title={`Delete save point #${sp.save_point_nr}?`}
                                    onClick={() => void deleteSave(sp)}
                                  >
                                    Confirm
                                  </Button>
                                ) : (
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    disabled={running || !sp.uuid}
                                    onClick={() => setConfirmDeleteSave(sp.dir_name)}
                                  >
                                    Delete
                                  </Button>
                                )}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                );
              })}
            </div>
          ))
        )}
      </section>

      <section className="grid gap-3">
        <div className="flex items-center justify-between">
          <h3 className="font-display text-sm font-bold uppercase tracking-widest text-stone-300">
            Snapshots
          </h3>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setUploadFile(null);
              setUploadLabel("");
              setUploadRestoreNow(false);
              setUploadErr(null);
              setUploadOpen(true);
            }}
          >
            Upload
          </Button>
        </div>
        {data.snapshots.length === 0 ? (
          <Empty label="No snapshots yet. Snapshot a save point above, or upload one." />
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-stone-800 text-left text-xs uppercase tracking-wider text-stone-400">
                <th className="py-2 pr-3">Label</th>
                <th className="py-2 pr-3">Created</th>
                <th className="py-2 pr-3">Source</th>
                <th className="py-2 pr-3">Size</th>
                <th className="py-2 pr-3">Mods</th>
                <th className="py-2 pr-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.snapshots.map((snap) => (
                <tr key={snap.snapshot_id} className="border-b border-stone-800">
                  <td className="py-2 pr-3 text-xs text-stone-100">{snap.label}</td>
                  <td className="py-2 pr-3 text-xs text-stone-400">{fmtDate(snap.created_at)}</td>
                  <td className="py-2 pr-3 text-xs text-stone-400">
                    {snap.playthrough_nr !== null && snap.save_point_nr !== null
                      ? `playthrough ${snap.playthrough_nr} · save point ${snap.save_point_nr}`
                      : "uploaded"}
                  </td>
                  <td className="py-2 pr-3 font-mono text-xs text-stone-400">
                    {fmtBytes(snap.archive_size_bytes ?? snap.uncompressed_size_bytes)}
                  </td>
                  <td className="py-2 pr-3 text-xs text-stone-400">{snap.mods.length} mods recorded</td>
                  <td className="py-2 pr-3 text-right">
                    <span className="inline-flex flex-wrap items-center justify-end gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={running}
                        onClick={() => {
                          setRestoreArm(true);
                          setRestoreErr(null);
                          setRestoreDialog(snap);
                        }}
                      >
                        Restore
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => void downloadSnapshot(snap)}>
                        Download
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={running}
                        onClick={() => {
                          setRenameLabel(snap.label);
                          setRenameErr(null);
                          setRenameSnapshot(snap);
                        }}
                      >
                        Rename
                      </Button>
                      {confirmDeleteSnapshot === snap.snapshot_id ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={running}
                          title={`Delete snapshot "${snap.label}"?`}
                          onClick={() => void deleteSnapshot(snap)}
                        >
                          Confirm
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={running}
                          onClick={() => setConfirmDeleteSnapshot(snap.snapshot_id)}
                        >
                          Delete
                        </Button>
                      )}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* Arm dialog — only opened while the server is running */}
      <Dialog
        open={armDialogSave !== null}
        title={armDialogSave ? `Load save point #${armDialogSave.save_point_nr}` : ""}
        onClose={() => !armBusy && setArmDialogSave(null)}
      >
        <div className="grid gap-3">
          <p className="text-xs text-stone-300">
            The server is running. Arm this save for the next restart, or restart now to load it
            immediately.
          </p>
          <label className="flex items-center gap-2 text-xs text-stone-300">
            <input
              type="checkbox"
              checked={armSticky}
              onChange={(e) => setArmSticky(e.target.checked)}
            />
            Keep this armed across every future restart (sticky)
          </label>
          {armErr && <p className="error">{armErr}</p>}
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={armBusy}
              onClick={() => setArmDialogSave(null)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={armBusy}
              onClick={() => void armForNextRestart()}
            >
              Arm for next restart
            </Button>
            <Button
              type="button"
              variant="default"
              disabled={armBusy || Boolean(action)}
              onClick={() => void restartNowAndLoad()}
            >
              {armBusy || action === "restart-load" ? "Working..." : "Restart now and load"}
            </Button>
          </div>
        </div>
      </Dialog>

      <Dialog
        open={snapshotDialogSave !== null}
        title={snapshotDialogSave ? `Snapshot save point #${snapshotDialogSave.save_point_nr}` : ""}
        onClose={() => !snapshotBusy && setSnapshotDialogSave(null)}
      >
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submitSnapshot();
          }}
        >
          <Input
            autoFocus
            placeholder="Label"
            value={snapshotLabel}
            onChange={(e) => setSnapshotLabel(e.target.value)}
          />
          {snapshotErr && <p className="error">{snapshotErr}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={snapshotBusy}
              onClick={() => setSnapshotDialogSave(null)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={snapshotBusy || !snapshotLabel.trim()}>
              {snapshotBusy ? "Snapshotting..." : "Snapshot"}
            </Button>
          </div>
        </form>
      </Dialog>

      <Dialog
        open={restoreDialog !== null}
        title={restoreDialog ? `Restore "${restoreDialog.label}"` : ""}
        onClose={() => !restoreBusy && setRestoreDialog(null)}
      >
        {restoreDialog && (
          <div className="grid gap-3">
            <p className="text-xs text-stone-300">
              Created {fmtDate(restoreDialog.created_at)}
              {restoreDialog.playthrough_nr !== null && restoreDialog.save_point_nr !== null
                ? ` · playthrough ${restoreDialog.playthrough_nr} · save point ${restoreDialog.save_point_nr}`
                : ""}
              {restoreDialog.game_version ? ` · ${restoreDialog.game_version}` : ""}
              {" · "}
              {restoreDialog.mods.length} mods recorded
            </p>
            {slotOccupied(data, restoreDialog) && (
              <p className="text-xs text-amber-300">
                A save point already occupies this slot. Restoring will replace it there (the existing
                one is automatically snapshotted first).
              </p>
            )}
            <label className="flex items-center gap-2 text-xs text-stone-300">
              <input
                type="checkbox"
                checked={restoreArm}
                onChange={(e) => setRestoreArm(e.target.checked)}
              />
              Load this save on next start
            </label>
            {restoreErr && <p className="error">{restoreErr}</p>}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                disabled={restoreBusy}
                onClick={() => setRestoreDialog(null)}
              >
                Cancel
              </Button>
              <Button type="button" disabled={restoreBusy} onClick={() => void submitRestore()}>
                {restoreBusy ? "Restoring..." : "Restore"}
              </Button>
            </div>
          </div>
        )}
      </Dialog>

      <Dialog
        open={renameSnapshot !== null}
        title={renameSnapshot ? `Rename "${renameSnapshot.label}"` : ""}
        onClose={() => !renameBusy && setRenameSnapshot(null)}
      >
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submitRenameSnapshot();
          }}
        >
          <Input autoFocus value={renameLabel} onChange={(e) => setRenameLabel(e.target.value)} />
          {renameErr && <p className="error">{renameErr}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={renameBusy}
              onClick={() => setRenameSnapshot(null)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={renameBusy || !renameLabel.trim()}>
              {renameBusy ? "Renaming..." : "Rename"}
            </Button>
          </div>
        </form>
      </Dialog>

      <Dialog open={uploadOpen} title="Upload snapshot" onClose={() => !uploadBusy && setUploadOpen(false)}>
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submitUpload();
          }}
        >
          <input
            type="file"
            accept=".tar.gz,.tgz"
            onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
            className="text-xs text-stone-300"
          />
          <Input
            placeholder="Label"
            value={uploadLabel}
            onChange={(e) => setUploadLabel(e.target.value)}
          />
          <label className="flex items-center gap-2 text-xs text-stone-300">
            <input
              type="checkbox"
              checked={uploadRestoreNow}
              onChange={(e) => setUploadRestoreNow(e.target.checked)}
            />
            Restore it immediately after upload
          </label>
          {uploadErr && <p className="error">{uploadErr}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              disabled={uploadBusy}
              onClick={() => setUploadOpen(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={uploadBusy || !uploadFile || !uploadLabel.trim()}>
              {uploadBusy ? "Uploading..." : "Upload"}
            </Button>
          </div>
        </form>
      </Dialog>
    </div>
  );
}
