import { Menu, Moon, ServerCog, Sun, X } from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useSession, useTheme } from "../providers";
import { Button } from "./ui";

const navigation = [
  { to: "/", label: "Dashboard" },
  { to: "/servers", label: "Servers" },
  { to: "/mods", label: "Mod Library" },
  { to: "/modpacks", label: "Modpacks" },
  { to: "/jobs", label: "Jobs" },
  { to: "/settings", label: "Settings" },
];

export function Shell() {
  const { user, logout } = useSession();
  const { dark, toggle } = useTheme();
  const [open, setOpen] = useState(false);

  return (
    <div className="app-grid">
      <aside className={`sidebar ${open ? "open" : ""}`}>
        <div className="brand">
          <ServerCog size={23} />
          <span>REFORGER</span>
          <small>OPS // 01</small>
        </div>
        <nav>
          {navigation.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"} onClick={() => setOpen(false)}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span>{user?.username}</span>
          <Button variant="ghost" size="sm" onClick={logout}>
            Sign out
          </Button>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            aria-label="Toggle navigation"
            onClick={() => setOpen((value) => !value)}
          >
            {open ? <X size={18} /> : <Menu size={18} />}
          </Button>
          <div className="topbar-status">
            <span className="signal" /> HOST LINK / SECURE
          </div>
          <Button variant="ghost" size="icon" aria-label="Toggle color theme" onClick={toggle}>
            {dark ? <Sun size={17} /> : <Moon size={17} />}
          </Button>
        </header>
        <div className="page">
          <Outlet />
        </div>
      </main>
    </div>
  );
}