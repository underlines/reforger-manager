import { Button, Card, CardContent, CardHeader, CardTitle } from "../ui";
import { useTheme } from "../../providers";

export function AppearanceCard() {
  const { dark, toggle } = useTheme();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Appearance</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <p>
          Current interface: <strong>{dark ? "dark tactical" : "light field"}</strong> mode. This preference is
          stored in this browser.
        </p>
        <div>
          <Button variant="outline" onClick={toggle} aria-pressed={dark}>
            Use {dark ? "light" : "dark"} theme
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}