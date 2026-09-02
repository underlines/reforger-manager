# Reforger Manager Frontend

Phase 4 React/Vite SPA for the Reforger operations console.

Use an npm cache and install location on the local Windows disk so `node_modules` is never written to the SMB share:

```powershell
$env:npm_config_cache = "C:\Users\Jan\AppData\Local\Temp\opencode\npm-cache"
$env:npm_config_prefix = "C:\Users\Jan\AppData\Local\Temp\opencode\npm-prefix"
npm install --prefix "C:\Users\Jan\AppData\Local\Temp\opencode\reforger-manager-frontend" --package-lock-only
```

For development and builds, link or install dependencies in a local worktree copy of this directory, then run:

```powershell
npm run dev
npm run build
```

The Vite dev server proxies `/api` and WebSockets to `http://127.0.0.1:18090`. JWTs are stored only in browser `sessionStorage`; local storage is used only for the light/dark theme preference.
