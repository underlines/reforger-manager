import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { DashboardPage } from "./pages/Dashboard";
import { JobsPage } from "./pages/Jobs";
import { LoginPage } from "./pages/Login";
import { ModpacksPage } from "./pages/Modpacks";
import { ModDetailPage } from "./pages/ModDetail";
import { ModsPage } from "./pages/Mods";
import { ServerDetailPage } from "./pages/ServerDetail";
import { ServersPage } from "./pages/Servers";
import { SettingsPage } from "./pages/Settings";
import { useSession } from "./providers";

export function App() {
  const { ready, user } = useSession();
  if (!ready) return <div className="boot">Initializing command console...</div>;

  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage />} />
      <Route element={user ? <Shell /> : <Navigate to="/login" replace />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/servers" element={<ServersPage />} />
        <Route path="/servers/:id" element={<ServerDetailPage />} />
        <Route path="/mods" element={<ModsPage />} />
        <Route path="/mods/:guid" element={<ModDetailPage />} />
        <Route path="/modpacks" element={<ModpacksPage />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}