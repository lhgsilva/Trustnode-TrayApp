# Trustnode Edge - Presentation Slides

## Slide 1 - Executive Pitch

### Headline
Trustnode Edge is a secure Industrial Data Gateway that connects OT machine data to IT and cloud systems in real time.

### The Problem
- PLC data is trapped in silos across protocols, plants, and legacy systems.
- IT teams need reliable, structured data for analytics and integration.
- OT teams need low-latency local operation without cloud dependency.

### Our Solution
- One edge runtime for PLC collection, local monitoring, and cloud sync.
- Real-time dashboards + historian + alarms + reporting in one platform.
- Hybrid architecture: local-first operation with cloud visibility.

### Business Value
- Faster deployment and lower integration cost.
- Higher uptime with store-and-forward resilience.
- Better decisions with live + historical context.
- Scales from pilot machine to multi-site operations.

### Why It Wins
- Built for both controls engineers and IT teams.
- Protocol-aware OT ingestion with modern data pipelines.
- Operationally practical: deploy, monitor, troubleshoot, expand.

---

## Slide 2 - IT/OT Security Architecture

### Security-by-Design Principles
- Edge-first acquisition: machine communication remains in OT.
- Controlled outbound sync to cloud/data platforms.
- No requirement for inbound exposure of plant network.

### Core Safety Controls
- Role-based access control (admin/operator permissions).
- TLS-enabled communication for API/database transport.
- Health checks, runtime status, and explicit failure visibility.
- Config audit trail and centralized app logs for traceability.

### Reliability + Data Integrity
- Store-and-forward buffering during outages.
- Sync recovery with backlog tracking and error reporting.
- Separation of live status, historian data, and config domains.

### IT/OT Governance Fit
- Supports network segmentation between OT and IT zones.
- Local autonomy for operations; centralized oversight for IT.
- Designed for phased rollout: pilot, validate, scale.

### Risk Reduction Outcomes
- Lower risk of production disruption.
- Reduced blind spots in gateway/device/database health.
- Faster incident triage with actionable diagnostics.

