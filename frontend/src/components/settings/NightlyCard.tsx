import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Button, Card, CardContent, CardHeader, CardTitle, Input } from "../ui";
import { api, type AppSettings } from "../../lib/api";

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

export function NightlyCard() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<AppSettings>("/api/settings") });
  const [enabled, setEnabled] = useState(false);
  const [hourText, setHourText] = useState("3");

  useEffect(() => {
    if (settings.data) {
      setEnabled(settings.data.nightly_check_enabled);
      setHourText(String(settings.data.nightly_check_hour));
    }
  }, [settings.data]);

  const parsedHour = Number.parseInt(hourText, 10);
  const hourValid = Number.isInteger(parsedHour) && parsedHour >= 0 && parsedHour <= 23;
  const save = useMutation({
    mutationFn: () =>
      api<AppSettings>("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ nightly_check_enabled: enabled, nightly_check_hour: parsedHour }),
      }),
    onSuccess: (result) => queryClient.setQueryData(["settings"], result),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Nightly Update Check</CardTitle>
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
              Once per day the backend checks the engine build and the mod library for updates. Applies
              immediately when saved; the state survives a backend restart.
            </p>
            <label className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              Enable nightly check
            </label>
            <label className="grid gap-1 text-xs">
              Hour of day (0–23, {""}
              backend timezone)
              <Input
                type="number"
                min={0}
                max={23}
                value={hourText}
                onChange={(event) => setHourText(event.target.value)}
                className="max-w-24"
                aria-label="Nightly check hour"
              />
            </label>
            {!hourValid && (
              <p className="error" role="alert">
                Hour must be a whole number between 0 and 23.
              </p>
            )}
            {save.isError && (
              <p className="error" role="alert">
                Save failed: {errorMessage(save.error)}
              </p>
            )}
            {save.isSuccess && (
              <p className="text-xs text-emerald-700 dark:text-emerald-300" role="status">
                Saved. The scheduler now reflects the new setting.
              </p>
            )}
            <div>
              <Button
                size="sm"
                onClick={() => {
                  save.reset();
                  save.mutate();
                }}
                disabled={!hourValid || save.isPending || settings.isLoading}
              >
                {save.isPending ? "Saving..." : "Save nightly check"}
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
