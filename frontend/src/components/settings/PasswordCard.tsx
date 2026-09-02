import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, CardContent, CardHeader, CardTitle, Input } from "../ui";
import { api } from "../../lib/api";

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

type PasswordChangeResponse = { status: string };

export function PasswordCard() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changed, setChanged] = useState(false);

  const change = useMutation({
    mutationFn: () =>
      api<PasswordChangeResponse>("/api/auth/password", {
        method: "POST",
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      }),
    onSuccess: () => {
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setChanged(true);
    },
  });
  const mismatch = confirmPassword.length > 0 && newPassword !== confirmPassword;
  const canSubmit =
    currentPassword.length > 0 && newPassword.length >= 8 && newPassword === confirmPassword;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Change Password</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <p>
          Updates the operator account's password. Your current session stays signed in; other sessions
          keep working until their token expires.
        </p>
        <form
          className="grid gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            change.reset();
            change.mutate();
          }}
        >
          <label className="grid gap-1 text-xs">
            Current password
            <Input
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
            />
          </label>
          <label className="grid gap-1 text-xs">
            New password (minimum 8 characters)
            <Input
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
            />
          </label>
          <label className="grid gap-1 text-xs">
            Confirm new password
            <Input
              type="password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
            />
          </label>
          {mismatch && (
            <p className="error" role="alert">
              The passwords do not match.
            </p>
          )}
          {change.isError && (
            <p className="error" role="alert">
              {errorMessage(change.error)}
            </p>
          )}
          {changed && change.isSuccess && (
            <p className="text-xs text-emerald-700 dark:text-emerald-300" role="status">
              Password changed. Use it the next time you sign in.
            </p>
          )}
          <div>
            <Button type="submit" size="sm" disabled={!canSubmit || change.isPending}>
              {change.isPending ? "Changing..." : "Change password"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
