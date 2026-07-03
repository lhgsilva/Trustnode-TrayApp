# TrustNode Intelligence

Self-contained AI-assistant module for TrustNode Edge. Users ask natural-
language questions about their process data; the assistant uses a small
fixed catalog of read-only tools to answer truthfully — no free-form SQL,
no hallucinated numbers.

This folder is **everything the module needs**. Drop it into an existing
Edge install and run `INSTALL.ps1` to wire it up.

## What you get

- **Chat** page — multi-chat history, local-vs-cloud data source toggle.
- **Insights** page — promote useful chat queries to named insights, run on
  schedule, deliver by email.
- **License-gated** — controlled per-customer from the developer portal.
- **AI endpoint configurable from the portal** — swap VPS, model, or provider
  (Ollama → vLLM → OpenAI) without rebuilding the Edge.

## Files

```
backend/             FastAPI router + service + Ollama client + tools
frontend/            React components (menu, chat page, insights page)
docs/                ARCHITECTURE.md, VPS_SETUP.md, PORTAL_LICENSE_FIELDS.md
INSTALL.ps1          Installer for existing Edge systems
```

## Install on a fresh Edge (you're building from this repo)

The build script picks the module up automatically because:

- The backend `main.py` imports `trustnode_intelligence.backend.router`.
- The frontend `App.jsx` imports `IntelligenceMenu`, `IntelligenceChatPage`,
  `IntelligenceInsightsPage` from `../../trustnode_intelligence/frontend/`.
- The license inspector recognises the `trustnode_intelligence` module key.

Build the EXE normally (`scripts/build-release.ps1`) and the module ships.

## Install on an already-deployed Edge

Run `INSTALL.ps1` from this folder. It will:

1. Copy `backend/` to `<edge>/backend/app/modules/trustnode_intelligence/`.
2. Add the import + `app.include_router(...)` line to `<edge>/backend/app/main.py`.
3. Copy `frontend/` into `<edge>/frontend/src/components/Intelligence/`.
4. Add the menu mount + page routes to `<edge>/frontend/src/App.jsx`.
5. Add `trustnode_intelligence` to `<edge>/backend/app/services/license_inspect.py`.
6. Rebuild the EXE (calls the existing build script).

The script makes a backup of any file it modifies (`.bak.<timestamp>`).

## License config (portal-side)

See `docs/PORTAL_LICENSE_FIELDS.md`. The portal pushes a JSON blob under
`module_configs.trustnode_intelligence`:

```json
{
  "endpoint_url": "https://ai.trustnode.lsapps.app",
  "model": "qwen2.5:7b-instruct",
  "auth_token": "...",
  "rate_limits": {"queries_per_day": 500, "max_tokens_per_query": 2048},
  "features": {"insights": true, "email_schedule": true},
  "allowed_tools": ["read_only"]
}
```

When `endpoint_url` is empty (default for new licenses), the Chat page
shows "AI endpoint not configured — ask your administrator". No requests
hit the LLM until the portal publishes a real URL.

## VPS setup

See `docs/VPS_SETUP.md`. One Ollama install per VPS handles all your
customers (their Edges call it with their license-scoped token).
