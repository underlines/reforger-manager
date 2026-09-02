import { type DetailServer } from "../../lib/api";
import { ServerForm } from "./ServerForm";

export function ConfigPanel({ server }: { id: string; server: DetailServer }) {
  return <ServerForm mode="edit" server={server} />;
}
