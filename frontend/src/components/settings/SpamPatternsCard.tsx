import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Button, Card, CardContent, CardHeader, CardTitle, Input } from "../ui";
import { api, type AppSettings } from "../../lib/api";

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

export function SpamPatternsCard() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<AppSettings>("/api/settings") });
  const [patterns, setPatterns] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [initialised, setInitialised] = useState(false);

  useEffect(() => {
    if (settings.data && !initialised) {
      setPatterns(settings.data.log_spam_patterns);
      setInitialised(true);
    }
  }, [settings.data, initialised]);

  const save = useMutation({
    mutationFn: () =>
      api<AppSettings>("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ log_spam_patterns: patterns }),
      }),
    onSuccess: (result) => queryClient.setQueryData(["settings"], result),
  });
  const addPattern = () => {
    const value = draft.trim().toLowerCase();
    setDraft("");
    if (!value || patterns.includes(value)) return;
    setPatterns([...patterns, value]);
  };

  return (
    <Card className="md:col-span-2">
      <CardHeader>
        <CardTitle>Log Spam Patterns</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        {settings.isLoading && <p role="status">Loading settings...</p>}
        {settings.isError && (
          <p className="error" role="alert">
            Could not load settings: {errorMessage(settings.error)}
          </p>
        )}
        {settings.data && (
          <>
            <p>
              Server log lines containing any of these substrings (case-insensitive) are filtered by the
              backend itself — both the console WebSocket stream and the stored log. The filter applies as
              soon as you save.
            </p>
            {patterns.length === 0 ? (
              <p>No patterns configured; every log line is shown.</p>
            ) : (
              <ul className="grid gap-1">
                {patterns.map((pattern, index) => (
                  <li key={pattern} className="flex items-center justify-between gap-2 text-xs">
                    <code className="break-all">{pattern}</code>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setPatterns(patterns.filter((_, i) => i !== index))}
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            <div className="flex items-end gap-2">
              <label className="grid flex-1 gap-1 text-xs">
                Add pattern
                <Input
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      addPattern();
                    }
                  }}
                  placeholder="e.g. deprecated function"
                />
              </label>
              <Button size="sm" variant="outline" onClick={addPattern} disabled={!draft.trim()}>
                Add
              </Button>
            </div>
            {save.isError && (
              <p className="error" role="alert">
                Save failed: {errorMessage(save.error)}
              </p>
            )}
            {save.isSuccess && (
              <p className="text-xs text-emerald-700 dark:text-emerald-300" role="status">
                Saved. Matching lines are now filtered server-side.
              </p>
            )}
            <div>
              <Button
                size="sm"
                onClick={() => {
                  save.reset();
                  save.mutate();
                }}
                disabled={save.isPending || settings.isLoading}
              >
                {save.isPending ? "Saving..." : "Save patterns"}
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
