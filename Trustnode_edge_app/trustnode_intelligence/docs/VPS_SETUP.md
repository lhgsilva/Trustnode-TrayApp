# VPS Setup — Hosting the AI for TrustNode Intelligence

One VPS hosts the LLM for all your customers. Each customer's Edge calls it
with a license-scoped token. Recommended sizing: 4 vCPU, 8 GB RAM, 30 GB
disk for the model.

## 1. Install Ollama on Ubuntu (or any Linux)

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

This installs Ollama as a systemd service listening on `localhost:11434`.

## 2. Pull the recommended model

```bash
ollama pull qwen2.5:7b-instruct
```

This downloads ~4.5 GB. Qwen 2.5 7B is Apache 2.0 licensed, top of its
weight class on tool-calling benchmarks. Smaller alternatives:

- `qwen2.5:3b-instruct` — 2 GB, half the speed of 7B at half the RAM
- `llama3.2:3b-instruct` — 2 GB, Llama 3.2 license (free for commercial
  use up to 700M MAU — fine for SaaS)

## 3. Expose Ollama publicly behind a reverse proxy

Ollama itself binds to `localhost`. Put nginx in front with TLS:

```nginx
server {
    listen 443 ssl;
    server_name ai.trustnode.lsapps.app;

    ssl_certificate     /etc/letsencrypt/live/ai.trustnode.lsapps.app/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ai.trustnode.lsapps.app/privkey.pem;

    # Auth: require a bearer token. Replace with your validation logic.
    if ($http_authorization = "") { return 401; }

    location / {
        proxy_pass http://127.0.0.1:11434;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 180s;
    }
}
```

## 4. Token validation (recommended)

For production, replace the simple "is Authorization header present"
check with real validation against your control plane. A small FastAPI
shim in front of Ollama is the easiest path:

```python
# vps_auth.py — runs on :8443, proxies to localhost:11434 after token check
from fastapi import FastAPI, Header, HTTPException, Request
from starlette.responses import StreamingResponse
import httpx

app = FastAPI()

VALID_TOKENS = set()  # populate from your control-plane DB on startup

@app.post("/v1/chat/completions")
async def proxy(request: Request, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    if token not in VALID_TOKENS:
        raise HTTPException(401, "Invalid token")
    body = await request.body()
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post("http://127.0.0.1:11434/v1/chat/completions",
                              content=body,
                              headers={"Content-Type": "application/json"})
    return StreamingResponse(iter([r.content]), status_code=r.status_code,
                             media_type=r.headers.get("content-type", "application/json"))
```

## 5. Configure customers in the developer portal

For each customer license that should have the module:

1. Add `trustnode_intelligence` to their module list.
2. Set the module config:
   ```json
   {
     "endpoint_url": "https://ai.trustnode.lsapps.app",
     "model": "qwen2.5:7b-instruct",
     "auth_token": "<unique-token-for-this-customer>",
     "rate_limits": {"queries_per_day": 500, "max_tokens_per_query": 2048},
     "features": {"insights": true, "email_schedule": true},
     "allowed_tools": ["read_only"]
   }
   ```
3. Add the customer's `auth_token` to the VPS allowlist.

When you push the license bundle, the Edge picks up the config on its
next license refresh. No Edge rebuild needed.

## 6. Switching to a commercial model later

Same config blob. Examples:

**OpenAI:**
```json
{
  "endpoint_url": "https://api.openai.com",
  "model": "gpt-4o-mini",
  "auth_token": "sk-...",
  ...
}
```

**Anthropic** (requires a tiny VPS adapter — Anthropic isn't OpenAI-compat):
Run a translation layer on your VPS that takes OpenAI-shape requests and
re-shapes them for Anthropic's `/v1/messages`. Point Edges at that.

## 7. Monitoring

- `ollama ps` shows loaded models + RAM.
- `journalctl -u ollama -f` for runtime logs.
- nginx access log gives you per-customer query volume (filter by token).
- Per-customer rate limits are enforced Edge-side via the
  `queries_per_day` license config (when the Edge tracks counter — future
  revision).

## 8. Cost

Self-hosted Ollama on a $20/mo VPS handles 100s of customer queries/day
on a 7B model. The only ongoing cost is the VPS itself. Commercial APIs
(OpenAI / Anthropic) cost per token but require zero ops.
