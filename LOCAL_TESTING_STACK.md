# Trustnode Local Testing Stack

This guide lets you run the full local stack before deploying to VPS.

## One-click start

From project root (`Tray_app`):

```powershell
.\start-local-stack.ps1
```

Optional custom ports:

```powershell
.\start-local-stack.ps1 -BackendPort 8000 -FrontendPort 5173 -HtmlPort 8090 -PhpPort 8091
```

## What the script starts

1. Backend API (FastAPI)  
   Path: `Trustnode_edge_app/backend`  
   Command:
   `uvicorn app.main:app --host 127.0.0.1 --port 8000`

2. Frontend app (Vite)  
   Path: `Trustnode_edge_app/frontend`  
   Command:
   `npm run dev -- --host 127.0.0.1 --port 5173`

3. HTML client test host  
   Path: `Trustnode_edge_app/client_page_tests`  
   Command:
   `python -m http.server 8090`

4. PHP client test host  
   Path: `Trustnode_edge_app/client_page_tests`  
   Command:
   `php -S 127.0.0.1:8091`

## Local URLs

- Edge/Web UI: `http://127.0.0.1:5173`
- Developer Portal: `http://127.0.0.1:5173/portal`
- Backend health: `http://127.0.0.1:8000/api/health`

### Client test pages

- `http://127.0.0.1:8090/client_test.html`
- `http://127.0.0.1:8090/client_test_db_rest.html`
- `http://127.0.0.1:8090/client_test_db_php.html`
- `http://127.0.0.1:8091/client_test.php`
- `http://127.0.0.1:8091/client_test_db.php`

## Optional domain simulation (customer subdomains)

If you want to test host-based tenant context locally, add these entries to your Windows `hosts` file:

`C:\Windows\System32\drivers\etc\hosts`

```txt
127.0.0.1 trustnode.lsapps.app
127.0.0.1 customer-a-trustnode.lsapps.app
127.0.0.1 customer-b-trustnode.lsapps.app
127.0.0.1 customer-c-trustnode.lsapps.app
```

Then open:

- `http://trustnode.lsapps.app:5173`
- `http://customer-a-trustnode.lsapps.app:5173`
- `http://customer-b-trustnode.lsapps.app:5173`
- `http://customer-c-trustnode.lsapps.app:5173`

## Stop services

Each service runs in its own PowerShell window.  
Close each window to stop that service.

## Troubleshooting

1. Backend venv missing  
   Create it in `Trustnode_edge_app/backend`:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Frontend dependencies missing  
   In `Trustnode_edge_app/frontend`:

   ```powershell
   npm install
   ```

3. `php` not found  
   Install PHP and ensure `php.exe` is in `PATH`.

4. Port already in use  
   Start with different ports:

   ```powershell
   .\start-local-stack.ps1 -BackendPort 8002 -FrontendPort 5175 -HtmlPort 8092 -PhpPort 8093
   ```

## Important local paths

- Backend: `Trustnode_edge_app/backend/app/main.py`
- Frontend: `Trustnode_edge_app/frontend/src/App.jsx`
- Styles: `Trustnode_edge_app/frontend/src/styles.css`
- Control plane API: `Trustnode_edge_app/backend/app/routers/control_plane.py`
- Client test files: `Trustnode_edge_app/client_page_tests`
- Diagnostics scripts:
  - `Trustnode_edge_app/client_page_tests/diagnostics/run_all_diagnostics.ps1`
  - `Trustnode_edge_app/client_page_tests/diagnostics/run_client_diagnostics.py`
