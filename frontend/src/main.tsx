import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./app";
import { AppProviders, ThemeProvider } from "./providers";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <ThemeProvider>
        <AppProviders>
          <App />
        </AppProviders>
      </ThemeProvider>
    </BrowserRouter>
  </StrictMode>,
);