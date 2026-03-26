# TrustNode AI Agent System

This document describes the team of specialized AI agents available in this project. Each agent is a deep expert in a specific layer of the TrustNode platform and is designed to review, diagnose, and improve the system without breaking what is already working.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    COORDINATOR                          │
│         (Master Agent — orchestrates all others)        │
└────────────┬──────────┬──────────┬──────────┬───────────┘
             │          │          │          │
     ┌───────┴──┐  ┌────┴───┐  ┌──┴─────┐  ┌┴──────────┐
     │ BACKEND  │  │FRONTEND│  │DATABASE│  │ SECURITY  │
     └──────────┘  └────────┘  └────────┘  └───────────┘
             │          │          │
     ┌───────┴──┐  ┌────┴───┐
     │   API    │  │   UI   │  ┌─────────────┐
     └──────────┘  └────────┘  │ PERFORMANCE │
                                └─────────────┘
```

---

## Agents

### `coordinator` — Master Coordinator
**When to use:** Full system audit, planning major features, coordinating multi-layer changes, resolving conflicts between specialist findings.

**Invocation examples:**
- `"Run a full review of the system and create an improvement plan"`
- `"We want to add alarm escalation — coordinate the changes needed across backend, frontend, and database"`
- `"Something is wrong with historian data — help me diagnose which layer has the issue"`

**Capabilities:**
- Dispatches all specialist agents
- Triages findings by severity (Critical / High / Medium / Low)
- Resolves cross-layer conflicts
- Produces a sequenced Implementation Plan
- Enforces the "do not break working features" invariant throughout

---

### `backend-reviewer` — Python/FastAPI/PLC Backend
**When to use:** Backend bugs, PLC gateway reliability, async Python issues, historian pipeline, service quality.

**Covers:**
- FastAPI routers and services
- PLC drivers (pycomm3, pylogix, snap7, OPC-UA)
- Async correctness (event loop, executors)
- Data pipeline integrity
- Error handling and observability

---

### `frontend-reviewer` — React/Vite Frontend
**When to use:** Frontend bugs, rendering performance, WebSocket client logic, chart optimizations, build configuration.

**Covers:**
- React component architecture
- State management and re-render optimization
- WebSocket client handling
- Recharts performance with live data
- Bundle size and code splitting

---

### `database-reviewer` — Database & Historian
**When to use:** Slow queries, data integrity issues, retention policy problems, schema design, multi-sink architecture.

**Covers:**
- Historian schema (indexes, types, partitioning)
- Query performance and pagination
- SQLite app-store integrity (WAL, transactions, FK constraints)
- Retention policy safety (no premature deletion)
- Backup/restore correctness
- InfluxDB integration quality

---

### `security-reviewer` — Cybersecurity & OT/IT Security
**When to use:** Before production deployment, when adding new endpoints, after a security incident, or when reviewing credential/auth handling.

**Covers:**
- JWT authentication and session management
- CORS configuration (critical: wildcard in production)
- Input validation and injection prevention (SSRF, SQLi, path traversal)
- Secrets management
- OT-specific threats (unauthorized PLC writes, lateral movement)
- Dependency CVEs
- WebSocket security

**Important:** This agent applies IEC 62443 / NIST CSF principles. Security failures in TrustNode can have physical plant consequences.

---

### `api-reviewer` — REST & WebSocket API Design
**When to use:** Adding new endpoints, reviewing API contracts before release, checking backward compatibility, integration readiness assessment.

**Covers:**
- HTTP method semantics and status codes
- API versioning strategy
- Pydantic schema completeness and typing
- WebSocket protocol design and message schemas
- Idempotency and safe retry semantics
- OpenAPI spec quality
- Grafana/PowerBI/third-party integration readiness

---

### `ui-reviewer` — Operator/Manager UX
**When to use:** Designing new dashboard features, reviewing operator workflows, improving alarm display, assessing manager reporting UX.

**Covers:**
- Operator situational awareness (alarm visibility, live readings clarity)
- Process engineer configuration workflows
- Manager/executive KPI dashboard usability
- Industrial HMI design best practices
- Accessibility (color-blindness, keyboard navigation)
- Error and empty states

---

### `performance-reviewer` — Latency & Throughput
**When to use:** Diagnosing slow dashboard updates, PLC read latency issues, scaling to more tags/gateways, WebSocket bottlenecks.

**Covers:**
- PLC polling cycle time and tag batching
- Async bottlenecks (blocking calls in event loop)
- WebSocket broadcast efficiency and backpressure
- Historian write throughput (batch inserts vs. single-row)
- Frontend rendering pipeline (React re-renders, chart data windowing)
- Capacity limits (max tags, max WS clients)

**Key SLAs:**
- PLC polling cycle: < 100ms
- Alarm detection latency: < 500ms
- Dashboard refresh: < 1 second
- Historian write throughput: 1000+ tags/sec sustained

---

## How to Use the Agent System

### Quick Start — Full System Review

Ask the coordinator to run all agents:

```
Use the coordinator agent to run a full system review and produce an improvement plan.
```

### Focused Review — Single Layer

Ask a specialist agent directly:

```
Use the security-reviewer agent to audit the authentication system and CORS configuration.
```

```
Use the performance-reviewer agent to analyze the PLC polling loop for batching opportunities.
```

### Implementation — Safe Changes

Always use the coordinator for implementing changes that touch multiple files:

```
Use the coordinator agent to implement the database indexing improvements from the database-reviewer report,
starting with the lowest-risk changes.
```

---

## Critical Invariants (Never Break)

These are enforced by all agents and the coordinator:

| # | Invariant | Why |
|---|-----------|-----|
| 1 | PLC polling loops continue if API restarts | Plant data collection must never stop |
| 2 | Historian data is never silently dropped | Production data loss is unrecoverable |
| 3 | JWT auth protects all write endpoints | Unauthorized PLC gateway changes = safety risk |
| 4 | WebSocket streams reconnect gracefully | Operators must not lose their live view |
| 5 | Local SQLite is source of truth for config | Cloud outage must not disable edge operation |
| 6 | Cloud sync is non-blocking to local ops | Slow cloud must not delay PLC reads |

---

## Agent Files Location

All agent definitions are in `.claude/agents/`:

```
.claude/agents/
├── coordinator.md          ← Master orchestrator
├── backend-reviewer.md     ← FastAPI, PLC workers, Python
├── frontend-reviewer.md    ← React, Recharts, WebSocket client
├── database-reviewer.md    ← Schema, queries, historian, retention
├── security-reviewer.md    ← Auth, CORS, OT threats, CVEs
├── api-reviewer.md         ← REST design, WebSocket protocol, versioning
├── ui-reviewer.md          ← Operator UX, HMI best practices
└── performance-reviewer.md ← Latency, throughput, scalability
```

---

## TrustNode Technology Reference

| Layer | Technology | Notes |
|-------|-----------|-------|
| Backend | FastAPI 0.116 + Python 3.11 | Async, Pydantic v2 |
| PLC (AB) | pycomm3 1.2.14 + pylogix 1.0.3 | Dual-driver with fallback |
| PLC (Siemens) | python-snap7 2.0.2 | S7-300/400/1200/1500 |
| PLC (Generic) | opcua 0.98.13 | OPC-UA browse + read |
| Database (local) | SQLite (SQLAlchemy) | WAL mode recommended |
| Database (cloud) | PostgreSQL / MySQL / MSSQL | Multi-sink |
| Time-series alt | InfluxDB | Optional sink |
| Frontend | React 18 + Vite 5 | No TypeScript yet |
| Charts | Recharts 2.15 | Live + historical |
| Desktop | Electron + pystray | Tray shell |
| Auth | JWT HS256 + PBKDF2 | 12h expiry |
| Real-time | WebSocket (asyncio) | `/ws/stream`, `/ws/cloud-stream` |
