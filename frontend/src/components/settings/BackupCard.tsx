import { useMutation } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Button, Card, CardContent, CardHeader, CardTitle } from "../ui";
import { api } from "../../lib/api";

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

type OnConflict = "skip" | "replace";
type PlanAction = "create" | "replace" | "skip";
type PlanItem = { name: string; action: PlanAction; note?: string | null };
type BackupPlan = {
  dry_run: boolean;
  on_conflict: string;
  servers: PlanItem[];
  modpacks: PlanItem[];
};
type BackupDocument = {
  version: number;
  exported_at?: string | null;
  servers: unknown[];
  modpacks: unknown[];
};

const actionTone: Record<PlanAction, string> = {
  create: "text-emerald-700 dark:text-emerald-300",
  replace: "text-amber-700 dark:text-amber-300",
  skip: "text-slate-500 dark:text-slate-400",
};

function PlanTable({ title, items }: { title: string; items: PlanItem[] }) {
  if (items.length === 0) {
    return (
      <div className="text-xs">
        <strong>{title}:</strong> none in this backup.
      </div>
    );
  }
  return (
    <div className="grid gap-1 text-xs">
      <strong>{title}</strong>
      <ul className="grid gap-1">
        {items.map((item) => (
          <li key={item.name} className="flex items-baseline justify-between gap-2">
            <code className="break-all">{item.name}</code>
            <span className={actionTone[item.action]}>
              {item.action}
              {item.note ? ` — ${item.note}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function BackupCard() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [parsed, setParsed] = useState<BackupDocument | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [onConflict, setOnConflict] = useState<OnConflict>("skip");
  const [plan, setPlan] = useState<BackupPlan | null>(null);
  const [applied, setApplied] = useState<BackupPlan | null>(null);

  const download = useMutation({
    mutationFn: async () => {
      const doc = await api<BackupDocument>("/api/backup/export");
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `reforger-manager-backup-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      return doc;
    },
  });

  const preview = useMutation({
    mutationFn: (doc: BackupDocument) =>
      api<BackupPlan>("/api/backup/import?dry_run=true", {
        method: "POST",
        body: JSON.stringify(doc),
      }),
    onSuccess: (result) => {
      setPlan(result);
      setApplied(null);
    },
  });

  const confirm = useMutation({
    mutationFn: (doc: BackupDocument) =>
      api<BackupPlan>(`/api/backup/import?dry_run=false&on_conflict=${onConflict}`, {
        method: "POST",
        body: JSON.stringify(doc),
      }),
    onSuccess: (result) => setApplied(result),
  });

  const resetRestore = () => {
    setParsed(null);
    setFileName(null);
    setParseError(null);
    setPlan(null);
    setApplied(null);
    preview.reset();
    confirm.reset();
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const onFile = async (file: File) => {
    setParseError(null);
    setPlan(null);
    setApplied(null);
    preview.reset();
    confirm.reset();
    try {
      const doc = JSON.parse(await file.text()) as BackupDocument;
      if (!doc || !Array.isArray(doc.servers) || !Array.isArray(doc.modpacks)) {
        throw new Error("not a backup document (missing servers / modpacks arrays)");
      }
      setParsed(doc);
      setFileName(file.name);
      preview.mutate(doc);
    } catch (error) {
      setParsed(null);
      setFileName(file.name);
      setParseError(errorMessage(error));
    }
  };

  return (
    <Card className="md:col-span-2">
      <CardHeader>
        <CardTitle>Backup &amp; Restore</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-2">
          <p>
            Download every server definition and modpack as one JSON file. The mod library and engine
            install are not included — they rebuild from a scan / a fresh engine install.
          </p>
          <p className="error" role="note">
            The file contains the game, admin and RCON passwords in cleartext. A restore has to
            reproduce a server you can actually administer. Store it somewhere private.
          </p>
          {download.isError && (
            <p className="error" role="alert">
              Export failed: {errorMessage(download.error)}
            </p>
          )}
          <div>
            <Button
              size="sm"
              onClick={() => {
                download.reset();
                download.mutate();
              }}
              disabled={download.isPending}
            >
              {download.isPending ? "Preparing..." : "Download backup"}
            </Button>
          </div>
        </div>

        <hr className="border-slate-200 dark:border-slate-700" />

        <div className="grid gap-2">
          <p>
            Restore from a backup file. The plan below is a dry run — nothing changes until you
            confirm. Servers and modpacks are matched by name.
          </p>
          <label className="grid gap-1 text-xs">
            Backup file
            <input
              ref={fileInputRef}
              type="file"
              accept="application/json,.json"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void onFile(file);
              }}
            />
          </label>
          {fileName && <p className="text-xs">Loaded: {fileName}</p>}
          {parseError && (
            <p className="error" role="alert">
              Could not read file: {parseError}
            </p>
          )}
          {preview.isPending && <p role="status">Building plan...</p>}
          {preview.isError && (
            <p className="error" role="alert">
              Dry run failed: {errorMessage(preview.error)}
            </p>
          )}

          {plan && !applied && (
            <div className="grid gap-3 rounded border border-slate-200 p-3 text-xs dark:border-slate-700">
              <strong>Planned changes (dry run)</strong>
              <PlanTable title="Server definitions" items={plan.servers} />
              <PlanTable title="Modpacks" items={plan.modpacks} />
              <label className="grid gap-1">
                On name conflict
                <select
                  value={onConflict}
                  onChange={(event) => setOnConflict(event.target.value as OnConflict)}
                  className="max-w-40 rounded border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-600"
                >
                  <option value="skip">skip — keep the existing one</option>
                  <option value="replace">replace — overwrite it</option>
                </select>
              </label>
              <p>
                A running server is never overwritten, whatever you pick here. With <code>skip</code>{" "}
                only new names are added.
              </p>
              {confirm.isError && (
                <p className="error" role="alert">
                  Restore failed: {errorMessage(confirm.error)}
                </p>
              )}
              <div className="flex gap-2">
                <Button
                  size="sm"
                  onClick={() => {
                    if (parsed) {
                      confirm.reset();
                      confirm.mutate(parsed);
                    }
                  }}
                  disabled={!parsed || confirm.isPending}
                >
                  {confirm.isPending ? "Restoring..." : `Apply restore (${onConflict})`}
                </Button>
                <Button size="sm" variant="ghost" onClick={resetRestore}>
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {applied && (
            <div className="grid gap-3 rounded border border-emerald-300 p-3 text-xs dark:border-emerald-700">
              <strong className="text-emerald-700 dark:text-emerald-300">
                Restore applied (on conflict: {applied.on_conflict})
              </strong>
              <PlanTable title="Server definitions" items={applied.servers} />
              <PlanTable title="Modpacks" items={applied.modpacks} />
              <div>
                <Button size="sm" variant="ghost" onClick={resetRestore}>
                  Done
                </Button>
              </div>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
