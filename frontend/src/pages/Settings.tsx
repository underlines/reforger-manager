import { PageHeading } from "../components/PageHeading";
import { AppearanceCard } from "../components/settings/AppearanceCard";
import { BackupCard } from "../components/settings/BackupCard";
import { EngineCard } from "../components/settings/EngineCard";
import { HealthCard } from "../components/settings/HealthCard";
import { NightlyCard } from "../components/settings/NightlyCard";
import { PasswordCard } from "../components/settings/PasswordCard";
import { SessionCard } from "../components/settings/SessionCard";
import { SpamPatternsCard } from "../components/settings/SpamPatternsCard";

export function SettingsPage() {
  return (
    <>
      <PageHeading
        title="Console Settings"
        detail="Operator session, interface preferences, and backend maintenance controls."
      />
      <div className="settings">
        <SessionCard />
        <AppearanceCard />
        <NightlyCard />
        <PasswordCard />
        <EngineCard />
        <HealthCard />
        <SpamPatternsCard />
        <BackupCard />
      </div>
    </>
  );
}
