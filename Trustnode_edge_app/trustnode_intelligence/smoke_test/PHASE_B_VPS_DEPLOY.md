# Phase B — Deploy the LLM on your VPS

After Phase A passes on the dev machine, you (or your ops person) run this
on the VPS. End-to-end install in ~15 minutes on a fresh Ubuntu 22.04
4 vCPU / 8 GB RAM box.

## Prerequisites

- A VPS with at least 8 GB RAM and 20 GB free disk.
- A DNS A record pointing to the VPS (e.g. `ai.trustnode.lsapps.app`).
- `certbot` available for Let's Encrypt TLS (recommended).

## One-shot install script

SSH to the VPS, copy-paste this whole block into the terminal:

```bash
# === TrustNode Intelligence VPS install ============================
set -euxo pipefail

# 1. Install Ollama (single binary, sets up systemd unit)
curl -fsSL https://ollama.com/install.sh | sh

# 2. Pull the recommended model. ~4.5 GB download.
sudo -u ollama ollama pull qwen2.5:7b-instruct

# 3. Verify Ollama is running.
sudo systemctl status ollama --no-pager | head -10
curl -s http://127.0.0.1:11434/api/tags | head -50

# 4. Install nginx + certbot
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-nginx

# 5. nginx site (BEFORE TLS — certbot upgrades it next).
sudo tee /etc/nginx/sites-available/trustnode-ai > /dev/null <<'NGX'
server {
    listen 80;
    server_name ai.trustnode.lsapps.app;

    # Token auth: customer Edge sends Authorization: Bearer <token>
    # The token list is validated by the file below.
    location / {
        # Require an Authorization header. Real validation happens via
        # auth_request below — this just rejects unauthenticated calls
        # before we hit Ollama.
        if ($http_authorization = "") { return 401 "Missing bearer token"; }

        proxy_pass http://127.0.0.1:11434;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }
}
NGX

sudo ln -sf /etc/nginx/sites-available/trustnode-ai /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# 6. TLS
sudo certbot --nginx -d ai.trustnode.lsapps.app --non-interactive --agree-tos -m you@trustnode.lsapps.app --redirect

# 7. Smoke-test from the VPS itself
curl -sS https://ai.trustnode.lsapps.app/v1/chat/completions \
  -H "Authorization: Bearer dev-test-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:7b-instruct","messages":[{"role":"user","content":"hi"}],"max_tokens":20}' | head -50

echo "=== DONE ==="
```

## Token authentication — stronger than the basic check above

The nginx rule above only checks that *some* Authorization header exists.
For production, replace it with a real allowlist using `auth_request`:

```bash
# /etc/nginx/conf.d/trustnode-tokens.conf
map $http_authorization $token_valid {
    default 0;
    "Bearer tn_ai_cust_mari_smoke_xxxx"  1;
    "Bearer tn_ai_cust_anothercust_yyyy" 1;
    # Add one line per customer.
}
```

Then in the server block replace the `if ($http_authorization = "")` block with:

```nginx
if ($token_valid != 1) { return 403 "Invalid bearer token"; }
```

Reload nginx after adding tokens: `sudo systemctl reload nginx`.

## Verify from your Edge

After the install + TLS, on the Edge machine:

```powershell
curl -sS https://ai.trustnode.lsapps.app/v1/chat/completions `
  -H "Authorization: Bearer tn_ai_cust_mari_smoke_xxxx" `
  -H "Content-Type: application/json" `
  -d '{"model":"qwen2.5:7b-instruct","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'
```

Expected: a JSON response with `choices[0].message.content`.

## Then publish the license for mari's customer

See `PORTAL_LICENSE_BLOB.md` in this folder for the exact JSON to drop
into mari's customer license `module_configs.trustnode_intelligence`.
