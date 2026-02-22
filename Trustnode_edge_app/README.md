# Trustnode Edge App (Next Gen)

This folder contains a new separated version of the project with 3 layers:

- `backend`: FastAPI service for PLC acquisition/API/WebSocket
- `frontend`: React + Vite modern web UI
- `desktop`: Electron tray shell to run as a desktop tray app

## Recommended Run Order (Development)

1. Backend
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

2. Frontend
```powershell
cd frontend
npm install
npm run dev
```

3. Desktop shell (optional while developing)
```powershell
cd desktop
npm install
npm run dev
```

## Notes

- Current PLC service is scaffolded with mock streaming data.
- It is ready to plug your real `pylogix`, `snap7`, and `opcua` connectors.
- Desktop tray shell can start/stop backend process and show/hide UI window.

## Build Windows Installer (.exe)

From `Trustnode_edge_app` root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1
```

Output will be in:

- `desktop/dist/` (installer and unpacked artifacts)
