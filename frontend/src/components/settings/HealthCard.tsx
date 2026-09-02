import { useQuery } from "@tanstack/react-query";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "../ui";
import { apiClient } from "../../lib/api";

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

export function HealthCard() {
  const health = useQuery({ queryKey: ["health"], queryFn: apiClient.health, refetchInterval: 30_000 });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Backend Health</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex items-center gap-2">
          <Badge tone={health.data?.status === "ok" ? "good" : health.isError ? "bad" : "neutral"}>
            {health.data?.status === "ok" ? "Healthy" : health.isLoading ? "Checking" : "Unavailable"}
          </Badge>
          <span className="text-xs text-stone-500 dark:text-stone-400">Checked every 30 seconds.</span>
        </div>
        {health.isError && (
          <p className="error" role="alert">
            Health check failed: {errorMessage(health.error)}
          </p>
        )}
        <div>
          <Button size="sm" variant="outline" onClick={() => void health.refetch()} disabled={health.isFetching}>
            {health.isFetching ? "Checking..." : "Check now"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}