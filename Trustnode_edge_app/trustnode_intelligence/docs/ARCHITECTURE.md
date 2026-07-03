# Architecture

## Data flow

```
User asks question in Chat UI
        │
        ▼
React  POST /api/intelligence/chats/{id}/messages
        │
        ▼
FastAPI router (router.py)
        │
        ▼
service.run_chat_turn()
        │
        ├──► OllamaClient.chat(messages, tools=catalog)
        │        ▲                                │
        │        │                                ▼
        │        │                     {tool_calls: [...]} or {content: "..."}
        │        │                                │
        │        │                                ▼
        │        │                     if tool_calls present:
        │        │                       for each call:
        │        │                         tools.run_tool(name, args, {data_source})
        │        │                                │
        │        │                                ▼
        │        │                       results pushed back as 'tool' messages
        │        │                                │
        │        └────────────────────────────────┘ (loop, max 6 iterations)
        │
        ▼
Final text answer → persisted in ai_messages, returned to UI
```

## Storage

Two tables in `app_store.db` (the existing edge SQLite):

- `ai_chats(id, tenant_id, user_id, title, data_source, created_utc, updated_utc)`
- `ai_messages(id, chat_id, role, content, tool_calls_json, tool_results_json, created_utc)`
- `ai_insights(id, tenant_id, user_id, title, description, prompt, tool_plan_json, data_source, schedule_cron, email_to, last_run_utc, last_result, last_error, enabled, created_utc, updated_utc)`

No new database file. Migration is idempotent — `ensure_schema()` runs
`CREATE TABLE IF NOT EXISTS` on first call.

## Tool catalog

The LLM only ever calls functions listed in `tools/catalog.py`. Each tool
has a category (`read_only`, `can_run_batches`, ...). The license bundle
specifies `allowed_tools`; tools outside that list are filtered from the
schema sent to the LLM and rejected at execution time.

Today's tools are all `read_only`:

| Tool | What it does |
|---|---|
| `list_tags` | Tag catalog from gateway configs |
| `list_gateways` | Gateways + runtime status |
| `get_tag_summary` | min/max/avg/count/stddev over a window |
| `compare_periods` | Same tag, two windows, delta |
| `get_batch_summary` | One batch's stats (if batch_management installed) |
| `list_recent_batches` | Recent batches (if batch_management installed) |
| `list_recent_alarms` | Recent app_log rows |

Adding a new tool: drop a `run_<x>` function in `tools/`, register it in
`catalog.TOOL_CATALOG` with a category. Done.

## Data source: local vs cloud

The Chat page has a Local-DB / Cloud-DB pill toggle. The selection is
stored on the chat row and passed as `data_source` in every tool call's
context. Tools that read historian (`get_tag_summary`, `compare_periods`)
honour it by setting `prefer_cloud_reads` on the existing
`app_store.get_historian_rows_range()` call. No new DB connection —
reuses the edge's existing customer-DB sync target.

## Insights

An insight is a **saved tool plan + narration prompt**, not a saved chat
transcript. Replaying an insight runs the deterministic tools again with
fresh data, then asks the LLM to narrate the new results using the saved
prompt. Scheduled runs always produce fresh numbers, not stale text.

## Scheduling

`insight_scheduler.py` is a daemon thread that starts when the router
module imports. Lightweight 5-field cron parser, minute resolution.
Aligns to minute boundaries so we don't run twice in the same minute.

Email delivery reuses the existing reports module's SMTP config from
`app_store.get_bootstrap()['email_settings']`.

## License gating

`license.py` checks `app.services.license_inspect.get_license_summary()`
for module key `trustnode_intelligence`. The router has
`Depends(require_intelligence_license)` on every route — unlicensed
edges return 404 on the entire `/api/intelligence/*` surface. The
frontend menu silently hides itself when `getStatus()` reports
`licensed=false` (or 404s).

## Failure modes

- **AI endpoint unreachable**: error surfaces in the chat reply, no crash.
- **Endpoint not configured**: chat returns a friendly "configure in portal"
  message; menu still shows.
- **Tool returns error**: the LLM sees `{error: "..."}` and can either
  retry with different args or apologise to the user.
- **Iteration cap hit**: chat returns a notice that the LLM couldn't
  finish — user can rephrase.
- **Scheduler can't reach LLM**: stores error in `last_error`,
  visible on the Insights card.

## What this does NOT do

- No free-form SQL execution. Ever.
- No writes to the historian or config.
- No tool that can mutate a running gateway.
- No streaming today (added in a future revision once Ollama tool-calling
  + streaming UI is verified end-to-end).
