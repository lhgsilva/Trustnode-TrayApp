# Desktop Tray Shell (Electron)

## Purpose

- Runs the modern UI in a desktop window.
- Creates tray icon and menu actions.
- Starts/stops backend process as a sidecar.

## Dev

```powershell
npm install
npm run dev
```

## Build Installer

```powershell
npm install
npm run dist
```

Build steps executed by `dist`:

- Build frontend (`../frontend`)
- Build backend executable (`../backend/dist/trustnode-service.exe`)
- Package both outputs with `electron-builder`:
  - NSIS installer (`Setup ... .exe`)
  - Portable executable (no installation)

## Environment Overrides

- `TRUSTNODE_UI_URL`: UI URL (default `http://127.0.0.1:5173`)
- `TRUSTNODE_BACKEND_CMD`: backend command (default `python -m app`)

## Remote Frontend Without Rebuild

You can make the desktop app load a hosted frontend URL (instead of bundled local files) by editing:

- User override file: `%APPDATA%\\trustnode-edge-desktop\\ui-source.json`
- Bundled default file: `resources\\ui-source.json` (included in package)

Example:

```json
{
  "mode": "remote",
  "remoteUrl": "https://your-frontend-domain.example.com"
}
```

Behavior:

- If remote UI loads, app uses it.
- If remote UI fails, app falls back automatically to bundled local frontend.
