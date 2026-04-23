# Control Plane Deploy Checklist (2026-04-23)

## 1) Apply DB migration (cloud Postgres/Supabase)

```powershell
cd D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app
powershell -ExecutionPolicy Bypass -File .\scripts\apply-control-plane-migration.ps1 `
  -PostgresUrl "<postgresql://user:pass@host:5432/dbname?sslmode=require>"
```

## 2) Deploy backend code and restart service

```bash
cd /opt/trustnode-edge/app
git fetch --all
git reset --hard origin/main
cd /opt/trustnode-edge/app/Trustnode_edge_app/backend
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart trustnode-backend
systemctl status trustnode-backend --no-pager
curl -fsS http://127.0.0.1:8000/api/health
```

## 3) Deploy cloud frontend bundle

Source bundle:
- `Trustnode_edge_app/web_cloud_readonly/`

Build command:

```powershell
cd D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app
powershell -ExecutionPolicy Bypass -File .\scripts\build-web-cloud-readonly.ps1 `
  -CloudApiUrl "https://trustnode.lsapps.app" `
  -BasePath "/"
```

Copy `web_cloud_readonly/*` to the VPS web root for `https://trustnode.lsapps.app/`.

## 4) Smoke tests

1. Login admin: `POST /api/auth/login`
2. Check control plane context: `GET /api/control-plane/runtime-context`
3. Create scoped user: `POST /api/control-plane/users`
4. Login with created user.
5. Delete test user: `DELETE /api/control-plane/users/{username}`

## 5) Desktop build outputs

Generated files:
- `Trustnode_edge_app/desktop/dist/Trustnode Setup 0.1.0.exe`
- `Trustnode_edge_app/desktop/dist/Trustnode 0.1.0.exe`
- `Trustnode_edge_app/backend/dist/trustnode-service.exe`

