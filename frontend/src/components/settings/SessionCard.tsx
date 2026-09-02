import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "../ui";
import { useSession } from "../../providers";

export function SessionCard() {
  const { user, logout } = useSession();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Operator Session</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <dl className="definition">
          <dt>Operator</dt>
          <dd>{user?.username ?? "Unavailable"}</dd>
          <dt>Role</dt>
          <dd>
            <Badge tone={user?.is_admin ? "good" : "neutral"}>
              {user?.is_admin ? "Administrator" : "Operator"}
            </Badge>
          </dd>
          <dt>Account</dt>
          <dd>
            <Badge tone={user?.is_active ? "good" : "bad"}>{user?.is_active ? "Active" : "Inactive"}</Badge>
          </dd>
          <dt>Credentials</dt>
          <dd>Retained only for this browser tab.</dd>
        </dl>
        <div>
          <Button variant="outline" onClick={logout}>
            Sign out
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}