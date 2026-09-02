import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError, apiClient, setAccessToken, type User } from "./lib/api";

const TOKEN_KEY = "reforger.session.token";

type Session = {
  user: User | null;
  ready: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
};

const SessionContext = createContext<Session | null>(null);

export function useSession() {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be inside SessionProvider");
  return value;
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const logout = () => {
    sessionStorage.removeItem(TOKEN_KEY);
    setAccessToken(null);
    setUser(null);
  };
  useEffect(() => {
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (!token) {
      setReady(true);
      return;
    }
    setAccessToken(token);
    apiClient.me().then(setUser).catch(logout).finally(() => setReady(true));
  }, []);
  useEffect(() => {
    window.addEventListener("auth:expired", logout);
    return () => window.removeEventListener("auth:expired", logout);
  });
  const login = async (username: string, password: string) => {
    const result = await apiClient.login(username, password);
    sessionStorage.setItem(TOKEN_KEY, result.access_token);
    setAccessToken(result.access_token);
    setUser(await apiClient.me());
  };
  return (
    <SessionContext.Provider value={{ user, ready, login, logout }}>{children}</SessionContext.Provider>
  );
}

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error) => {
      if (!(error instanceof ApiError && error.status === 401)) {
        window.dispatchEvent(
          new CustomEvent("toast", {
            detail: { message: error instanceof Error ? error.message : "Request failed", tone: "bad" },
          }),
        );
      }
    },
  }),
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <SessionProvider>{children}</SessionProvider>
    </QueryClientProvider>
  );
}

const ThemeContext = createContext<{ dark: boolean; toggle: () => void } | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [dark, setDark] = useState(() => localStorage.getItem("reforger.theme") !== "light");
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("reforger.theme", dark ? "dark" : "light");
  }, [dark]);
  return (
    <ThemeContext.Provider value={{ dark, toggle: () => setDark((value) => !value) }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be inside ThemeProvider");
  return value;
}