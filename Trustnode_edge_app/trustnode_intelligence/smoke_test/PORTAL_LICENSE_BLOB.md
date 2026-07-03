# Portal license blob for mari's customer

Once Phase B (VPS install) is done and you have an HTTPS URL, paste this
JSON into mari's customer license in the developer portal — under
`license.module_configs.trustnode_intelligence`:

```json
{
  "endpoint_url": "https://ai.trustnode.lsapps.app",
  "model": "qwen2.5:7b-instruct",
  "auth_token": "tn_ai_cust_mari_smoke_xxxx",
  "rate_limits": {
    "queries_per_day": 500,
    "max_tokens_per_query": 2048
  },
  "features": {
    "insights": true,
    "email_schedule": true
  },
  "allowed_tools": ["read_only"]
}
```

Also ensure her license `modules` array contains `"trustnode_intelligence"`.

Full license object (relevant fields):

```json
{
  "package_key": "operations_plus_ai",
  "modules": [
    "batch_management",
    "reporting",
    "trustnode_intelligence"
  ],
  "module_configs": {
    "trustnode_intelligence": {
      "endpoint_url": "https://ai.trustnode.lsapps.app",
      "model": "qwen2.5:7b-instruct",
      "auth_token": "tn_ai_cust_mari_smoke_xxxx",
      "rate_limits": { "queries_per_day": 500, "max_tokens_per_query": 2048 },
      "features": { "insights": true, "email_schedule": true },
      "allowed_tools": ["read_only"]
    }
  }
}
```

After saving in the portal:
1. The portal pushes the license bundle to mari's edge.
2. Within ~30 s (license cache TTL), `/api/intelligence/status` on her edge will report `endpoint_configured: true`.
3. The "TrustNode Intelligence" menu group appears in her sidebar.
4. She can start chatting.

## Per-customer auth tokens

Generate a different `auth_token` for each customer:

```python
import secrets
print("tn_ai_cust_" + secrets.token_urlsafe(16))
```

Add the token to the nginx allowlist (see `PHASE_B_VPS_DEPLOY.md`). One
token per customer makes per-customer rate-limit / revocation possible.

## Rolling back

Remove `"trustnode_intelligence"` from `license.modules` (and from
`module_configs` for cleanliness). The next license refresh on the edge
will hide the menu and 404 the API.
