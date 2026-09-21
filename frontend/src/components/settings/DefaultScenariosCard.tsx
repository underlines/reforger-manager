import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Button, Card, CardContent, CardHeader, CardTitle, Input } from "../ui";
import { api, type AppSettings, type ScenarioDefault } from "../../lib/api";

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

export function DefaultScenariosCard() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<AppSettings>("/api/settings") });
  const [scenarios, setScenarios] = useState<ScenarioDefault[]>([]);
  const [draftName, setDraftName] = useState("");
  const [draftId, setDraftId] = useState("");
  const [initialised, setInitialised] = useState(false);

  useEffect(() => {
    if (settings.data && !initialised) {
      setScenarios(settings.data.default_scenarios);
      setInitialised(true);
    }
  }, [settings.data, initialised]);

  const save = useMutation({
    mutationFn: () =>
      api<AppSettings>("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ default_scenarios: scenarios }),
      }),
    onSuccess: (result) => queryClient.setQueryData(["settings"], result),
  });
  const addScenario = () => {
    const game_id = draftId.trim();
    const name = draftName.trim();
    if (!game_id || !name || scenarios.some((s) => s.game_id === game_id)) return;
    setDraftName("");
    setDraftId("");
    setScenarios([...scenarios, { game_id, name }]);
  };

  return (
    <Card className="md:col-span-2">
      <CardHeader>
        <CardTitle>Default Scenarios</CardTitle>
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
              Offered in every server definition&apos;s scenario picker, regardless of that
              definition&apos;s mods &mdash; seeded from Arma Reforger&apos;s official scenario list.
              Edit or remove entries and add your own; changes apply as soon as you save.
            </p>
            {scenarios.length === 0 ? (
              <p>No default scenarios configured; the picker only offers mod scenarios.</p>
            ) : (
              <ul className="grid gap-1">
                {scenarios.map((scenario, index) => (
                  <li
                    key={`${scenario.game_id}:${index}`}
                    className="flex items-center justify-between gap-2 text-xs"
                  >
                    <span className="min-w-0">
                      <span className="block truncate">{scenario.name}</span>
                      <code className="block truncate text-stone-500">{scenario.game_id}</code>
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setScenarios(scenarios.filter((_, i) => i !== index))}
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            <div className="grid gap-2 sm:grid-cols-[1fr_2fr_auto] sm:items-end">
              <label className="grid gap-1 text-xs">
                Name
                <Input
                  value={draftName}
                  onChange={(event) => setDraftName(event.target.value)}
                  placeholder="Conflict - Everon"
                />
              </label>
              <label className="grid gap-1 text-xs">
                Scenario id
                <Input
                  value={draftId}
                  onChange={(event) => setDraftId(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      addScenario();
                    }
                  }}
                  placeholder="{ECC61978EDCC2B5A}Missions/23_Campaign.conf"
                />
              </label>
              <Button
                size="sm"
                variant="outline"
                onClick={addScenario}
                disabled={!draftName.trim() || !draftId.trim()}
              >
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
                Saved. Server definitions now offer this list.
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
                {save.isPending ? "Saving..." : "Save default scenarios"}
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
