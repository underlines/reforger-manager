import { ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Button, Input } from "../components/ui";
import { useSession } from "../providers";

export function LoginPage() {
  const { login } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login">
      <form className="login-panel" onSubmit={submit}>
        <div className="brand">
          <ShieldCheck size={24} />
          <span>REFORGER</span>
          <small>SERVER CONTROL</small>
        </div>
        <p>Authenticate to access the operations console.</p>
        <label>
          Operator
          <Input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required />
        </label>
        <label>
          Access key
          <Input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <Button type="submit" disabled={busy} className="w-full">
          {busy ? "Authenticating" : "Enter console"}
        </Button>
        <small>Session token is retained only for this browser tab.</small>
      </form>
    </main>
  );
}