# Portal — Developer Portal fields for TrustNode Intelligence

The developer portal needs a small admin section for the
`trustnode_intelligence` module config. This document is what to build
on the portal side.

## Where it lives

Customer → License → Modules tab. When `trustnode_intelligence` is
enabled, show a "Configure" button that opens this panel.

## Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `endpoint_url` | URL | `""` | Where the Edge POSTs chat requests. Empty disables the module gracefully (UI shows "configure in portal"). |
| `model` | string | `qwen2.5:7b-instruct` | Model name. Must exist on the configured endpoint. |
| `auth_token` | password | `""` | Bearer token Edge sends with every request. Generate per-customer. |
| `rate_limits.queries_per_day` | int | `500` | Max LLM queries per day. |
| `rate_limits.max_tokens_per_query` | int | `2048` | Caps output length per response. |
| `features.insights` | bool | `true` | Hide the Insights submenu when off. |
| `features.email_schedule` | bool | `true` | Disable scheduled email delivery when off. |
| `allowed_tools` | string[] | `["read_only"]` | Categories of tools the LLM can call. Today only `read_only` exists. Future categories: `can_run_batches`, `can_export_reports`. |

## Serialization

Stored in the customer's license bundle under
`module_configs.trustnode_intelligence`. The Edge reads it via
`license_inspect.get_license_summary()['module_configs']`.

Example license bundle excerpt:

```json
{
  "package_key": "operations_plus_ai",
  "modules": ["batch_management", "reporting", "trustnode_intelligence"],
  "module_configs": {
    "trustnode_intelligence": {
      "endpoint_url": "https://ai.trustnode.lsapps.app",
      "model": "qwen2.5:7b-instruct",
      "auth_token": "tn_ai_cust_9f3a2b1c...",
      "rate_limits": {"queries_per_day": 500, "max_tokens_per_query": 2048},
      "features": {"insights": true, "email_schedule": true},
      "allowed_tools": ["read_only"]
    }
  }
}
```

## Edge-side reading

The Edge reads this via `license_inspect.get_license_summary()`. The
result is plumbed through `trustnode_intelligence.backend.license.get_module_config()`.

## Generating tokens

For now: random URL-safe string per customer, manually entered into the
VPS reverse-proxy allowlist. Future revision: portal generates + pushes
tokens automatically via a control-plane API.

## Suggested UI

```
┌─ TrustNode Intelligence ───────────────────────────────────┐
│                                                             │
│   AI endpoint URL  ┌─────────────────────────────────────┐ │
│                    │ https://ai.trustnode.lsapps.app     │ │
│                    └─────────────────────────────────────┘ │
│                                                             │
│   Model            ┌─────────────────────────────────────┐ │
│                    │ qwen2.5:7b-instruct                 │ │
│                    └─────────────────────────────────────┘ │
│                                                             │
│   Auth token       ┌─────────────────────────────────────┐ │
│                    │ ************************            │ │
│                    └─────────────────────────────────────┘ │
│   [ Generate new ]                                          │
│                                                             │
│   Rate limits                                               │
│   ┌─ Queries/day  ┐  ┌─ Tokens/query ┐                     │
│   │ 500           │  │ 2048           │                     │
│   └───────────────┘  └────────────────┘                     │
│                                                             │
│   Features                                                  │
│   [x] Insights submenu                                      │
│   [x] Email schedule                                        │
│                                                             │
│   Allowed tools                                             │
│   [x] Read-only data tools                                  │
│   [ ] Batch operations (future)                             │
│                                                             │
│              [ Cancel ]  [ Save ]                           │
└─────────────────────────────────────────────────────────────┘
```
