# Trustnode Edge CI/CD (One Command)

## What it does

On push to `main`, GitHub Actions will:

1. Build/test backend (`py_compile` smoke checks).
2. Build cloud web bundle (`web_cloud_readonly`).
3. Deploy code to VPS (`git pull` + backend restart).
4. Deploy web bundle to `/var/www/trustnode`.
5. Run smoke checks:
   - backend service active
   - `/api/health` (local + public)
   - `/api/app-store/live` (local + public)
   - frontend bundle contains `/api/app-store/live`

## Required GitHub repository secrets

- `VPS_HOST` (example: `203.0.113.10`)
- `VPS_USER` (example: `root`)
- `VPS_SSH_KEY` (private key for VPS access)
- `TRUSTNODE_CLOUD_API_URL` (example: `https://trustnode.lsapps.app`) optional; defaults to that URL

## One-command release from Windows

From repo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\Trustnode_edge_app\scripts\one-command-release.ps1 -CloudApiUrl "https://trustnode.lsapps.app" -CommitMessage "release: live cloud lane"
```

This command builds locally, commits, pushes to `main`, and triggers automatic CI/CD deploy + checks.

## Fast sync runtime env on VPS

Pipeline enforces these service env vars:

- `TRUSTNODE_PREFER_CLOUD_READS=true`
- `TRUSTNODE_CONFIG_SYNC_SECONDS=2`
- `TRUSTNODE_DATA_SYNC_BATCH_SIZE=5000`
- `TRUSTNODE_LIVE_SYNC_SAMPLE_ROWS=30000`
