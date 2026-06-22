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
- `TRUSTNODE_CONFIG_SYNC_SECONDS=1`
- `TRUSTNODE_DATA_BULK_SYNC_SECONDS=1`
- `TRUSTNODE_DATA_SYNC_BATCH_SIZE=500`

## Manual push scripts — safety rules (2026-06-21)

Two scripts let you SFTP a bundle to the VPS without waiting for CI:

- `scripts/push_portal_to_vps.py` — pushes `frontend/dist_cloud_readonly/` → `/var/www/trustnode/`
- `scripts/push_lite_to_vps.py` — pushes `web_cloud_readonly/lite/*` → `/var/www/trustnode/lite/`

Both refuse to run when there are **uncommitted changes** to:
- `frontend/src/` (the source the portal bundle is built from), or
- `frontend/dist_cloud_readonly/` (the build artifact must match git so we have a rollback point)

If you really need to ship an experimental build, pass `--force`. The bypass is logged loudly. Do not use `--force` against production unless you've tested the bundle in a non-production browser session first.

**Why the gate exists**: on 2026-06-21 we shipped a portal build that included 800+ lines of uncommitted source experimentation. It broke the master-admin developer portal in production. The committed bundle (`index-CX5xRUcF.js`, from Jun 18) was overwritten and had to be restored from `git HEAD`. The safety gate prevents this from happening silently.

## Nginx config

The production nginx vhost lives at `/etc/nginx/conf.d/trustnode-edge.conf` on the VPS. A mirror is kept at `deploy/nginx/trustnode-edge.conf` in this repo so:

- changes can be reviewed in a PR
- a fresh VPS can be provisioned by copying this file
- the `location ^~ /developer-portal/` block (added 2026-06-21) is preserved across deploys

The `developer-portal/` location is required because the dev-portal stub references its JS via absolute `/assets/...`. Without that location block, nginx falls through to the SPA root `index.html` for `/developer-portal/` requests, which uses relative `./assets/...` paths that 404 under the `/developer-portal/` URL. The browser then refuses to execute the JS module due to MIME-type mismatch.

If you change the nginx config on the VPS, mirror the change into `deploy/nginx/trustnode-edge.conf` and commit.

## Developer-portal stub

The PowerShell build script `scripts/build-web-cloud-readonly.ps1` generates `web_cloud_readonly/developer-portal/index.html` from the root index by:

1. Swapping the title `Trustnode Edge` → `Trustnode Developer Portal`
2. Rewriting `./assets/...` → `/assets/...` (absolute paths)

Step 2 was missing previously and only surfaced after a deploy that left no fallback. Do not remove step 2.
