import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Empty } from "../components/Empty";
import { PageHeading } from "../components/PageHeading";
import { ServerForm } from "../components/server/ServerForm";
import { ServerRow } from "../components/server/ServerRow";
import { Button, Card, CardContent, CardHeader, CardTitle } from "../components/ui";
import { apiClient } from "../lib/api";

export function ServersPage() {
  const query = useQuery({ queryKey: ["servers"], queryFn: apiClient.servers });
  const [creating, setCreating] = useState(false);
  // Favourites first. Array#sort is stable, so ties keep the API's (id) order.
  const servers = [...(query.data ?? [])].sort(
    (a, b) => Number(b.is_favourite) - Number(a.is_favourite),
  );

  return (
    <>
      <PageHeading
        title="Server Definitions"
        detail="One server can run at a time. Definitions remain ready for pre-flight and deployment."
        actions={
          <Button onClick={() => setCreating((value) => !value)}>
            {creating ? "Close" : "New definition"}
          </Button>
        }
      />
      {creating && (
        <Card className="mb-4">
          <CardHeader>
            <CardTitle>New Server Definition</CardTitle>
          </CardHeader>
          <CardContent>
            <ServerForm mode="create" onCancel={() => setCreating(false)} />
          </CardContent>
        </Card>
      )}
      <Card>
        <CardHeader>
          <CardTitle>Deployment Roster</CardTitle>
        </CardHeader>
        <CardContent>
          {query.isLoading ? (
            <p>Loading definitions...</p>
          ) : servers.length ? (
            <div className="list">
              {servers.map((server) => (
                <ServerRow key={server.id} server={server} />
              ))}
            </div>
          ) : (
            <Empty label="No server definitions have been created." />
          )}
        </CardContent>
      </Card>
    </>
  );
}
