# Trustnode Edge Backend

FastAPI service for PLC gateway control, status, and live stream.

## Start

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## Endpoints

- `GET /api/health`
- `GET /api/plc/config`
- `PUT /api/plc/config`
- `GET /api/plc/status`
- `GET /api/plc/snapshot`
- `POST /api/plc/start`
- `POST /api/plc/stop`
- `WS /ws/stream`
