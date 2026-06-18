"""Generate a PDF audit report covering the TrustNode codebase layout
and a recipe for shipping a customer-branded slice without exposing
sensitive backend code.

Run once with:
    python docs/customer_packaging_audit_2026-06-18.py
Output: docs/customer_packaging_audit_2026-06-18.pdf
"""
from __future__ import annotations

import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether, ListFlowable, ListItem,
)


HERE = Path(__file__).resolve().parent
OUT = HERE / "customer_packaging_audit_2026-06-18.pdf"

BRAND_NAVY = colors.HexColor("#1f3a5f")
BRAND_TEAL = colors.HexColor("#0e8479")
INK = colors.HexColor("#0f172a")
INK_SOFT = colors.HexColor("#64748b")
SURFACE = colors.HexColor("#f6f8fa")
WARN_BG = colors.HexColor("#fff7e6")
WARN_BORDER = colors.HexColor("#d97706")
DANGER_BG = colors.HexColor("#fef2f2")
DANGER_BORDER = colors.HexColor("#dc2626")
OK_BG = colors.HexColor("#ecfdf5")
OK_BORDER = colors.HexColor("#059669")

styles = getSampleStyleSheet()


def style(name, **kw):
    base = kw.pop("base", "Normal")
    s = ParagraphStyle(name, parent=styles[base], **kw)
    return s


H1 = style("H1", base="Heading1", fontName="Helvetica-Bold", fontSize=22,
           leading=26, textColor=BRAND_NAVY, spaceAfter=10, spaceBefore=6)
H2 = style("H2", base="Heading2", fontName="Helvetica-Bold", fontSize=15,
           leading=20, textColor=BRAND_NAVY, spaceAfter=8, spaceBefore=14)
H3 = style("H3", base="Heading3", fontName="Helvetica-Bold", fontSize=12,
           leading=16, textColor=BRAND_TEAL, spaceAfter=4, spaceBefore=10)
BODY = style("Body", fontName="Helvetica", fontSize=10, leading=14,
             textColor=INK, alignment=TA_LEFT, spaceAfter=6)
SUB = style("Sub", fontName="Helvetica-Oblique", fontSize=9, leading=12,
            textColor=INK_SOFT, spaceAfter=8)
CODE = style("Code", fontName="Courier", fontSize=9, leading=12,
             textColor=INK, leftIndent=8, spaceAfter=6, spaceBefore=4,
             backColor=SURFACE, borderColor=colors.HexColor("#e5e7eb"),
             borderPadding=6, borderWidth=0.5)
NOTE = style("Note", fontName="Helvetica", fontSize=9.5, leading=13,
             textColor=INK, leftIndent=8, rightIndent=8, spaceAfter=8,
             borderPadding=8, borderRadius=4)
CAPTION = style("Caption", fontName="Helvetica-Oblique", fontSize=8.5,
                leading=11, textColor=INK_SOFT, spaceAfter=10)


def boxed(text, kind="note"):
    """Render a colour-banded callout paragraph."""
    bg, border = SURFACE, colors.HexColor("#e5e7eb")
    if kind == "warn": bg, border = WARN_BG, WARN_BORDER
    if kind == "danger": bg, border = DANGER_BG, DANGER_BORDER
    if kind == "ok": bg, border = OK_BG, OK_BORDER
    s = ParagraphStyle(
        "Boxed", parent=NOTE, backColor=bg, borderColor=border,
        borderWidth=0.8, borderPadding=8,
    )
    return Paragraph(text, s)


def kv_table(rows, col_widths=None, header=True):
    """Simple 2-column key/value table."""
    if col_widths is None:
        col_widths = [5.5 * cm, 11.5 * cm]
    t = Table(rows, colWidths=col_widths, hAlign="LEFT")
    cmds = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
    ]
    if header:
        cmds += [
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9.5),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_NAVY),
        ]
    cmds += [("FONT", (0, 0 if not header else 1), (0, -1), "Helvetica-Bold", 9)]
    t.setStyle(TableStyle(cmds))
    return t


def file_table(rows):
    """File inventory table. rows = list of [Path, Purpose, Sensitivity, Ship to Customer]"""
    data = [["Path", "Purpose", "Sensitivity", "Ship to customer?"]] + rows
    t = Table(data, colWidths=[6.0 * cm, 6.0 * cm, 2.5 * cm, 2.7 * cm], hAlign="LEFT")
    cmds = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("FONT", (0, 1), (0, -1), "Courier", 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_NAVY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
    ]
    # Color the sensitivity column
    for i, row in enumerate(rows, start=1):
        sens = row[2].lower()
        if "high" in sens or "secret" in sens:
            cmds.append(("BACKGROUND", (2, i), (2, i), DANGER_BG))
            cmds.append(("TEXTCOLOR", (2, i), (2, i), DANGER_BORDER))
        elif "med" in sens:
            cmds.append(("BACKGROUND", (2, i), (2, i), WARN_BG))
            cmds.append(("TEXTCOLOR", (2, i), (2, i), WARN_BORDER))
        else:
            cmds.append(("BACKGROUND", (2, i), (2, i), OK_BG))
            cmds.append(("TEXTCOLOR", (2, i), (2, i), OK_BORDER))
        ship = row[3].lower()
        if ship.startswith("yes"):
            cmds.append(("BACKGROUND", (3, i), (3, i), OK_BG))
            cmds.append(("TEXTCOLOR", (3, i), (3, i), OK_BORDER))
        elif ship.startswith("no"):
            cmds.append(("BACKGROUND", (3, i), (3, i), DANGER_BG))
            cmds.append(("TEXTCOLOR", (3, i), (3, i), DANGER_BORDER))
        else:
            cmds.append(("BACKGROUND", (3, i), (3, i), WARN_BG))
            cmds.append(("TEXTCOLOR", (3, i), (3, i), WARN_BORDER))
    t.setStyle(TableStyle(cmds))
    return t


def page_decoration(canvas, doc):
    """Header band + footer page numbers on every page."""
    canvas.saveState()
    w, h = A4
    # Top brand band
    canvas.setFillColor(BRAND_NAVY)
    canvas.rect(0, h - 1.2 * cm, w, 1.2 * cm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(1.6 * cm, h - 0.78 * cm, "TrustNode — Codebase + Customer Packaging Audit")
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(w - 1.6 * cm, h - 0.78 * cm, "Operator confidential · 2026-06-18")
    # Footer
    canvas.setFillColor(INK_SOFT)
    canvas.setFont("Helvetica", 8.5)
    canvas.drawString(1.6 * cm, 1.0 * cm, "TrustNode Edge — packaging guidance for customer-branded Client View deployments")
    canvas.drawRightString(w - 1.6 * cm, 1.0 * cm, f"Page {doc.page}")
    canvas.restoreState()


def main():
    doc = SimpleDocTemplate(
        str(OUT), pagesize=A4,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
        topMargin=1.7 * cm, bottomMargin=1.5 * cm,
        title="TrustNode Codebase + Customer Packaging Audit",
        author="TrustNode Engineering",
    )

    elements = []

    # ── Cover ─────────────────────────────────────────────────────────
    elements += [
        Spacer(1, 4.5 * cm),
        Paragraph("TrustNode Edge", H1),
        Paragraph("Codebase Inventory & Customer-Branded Packaging Guide", H2),
        Spacer(1, 0.4 * cm),
        Paragraph("Audit date: 2026-06-18 · Audit scope: <i>complete repository</i>", SUB),
        Spacer(1, 0.4 * cm),
        Paragraph(
            "This report is a deep, read-only audit of the TrustNode codebase. "
            "It enumerates every directory and the role each plays, classifies files "
            "by sensitivity, and prescribes a maintainable recipe for shipping a "
            "<b>customer-branded Client View</b> deployment that points at the cloud "
            "TrustNode platform without exposing source code, secrets, or implementation "
            "internals to the customer.",
            BODY,
        ),
        Spacer(1, 0.6 * cm),
        boxed(
            "<b>Goal:</b> let the customer (or their web team) host a self-contained, "
            "TrustNode-branded read-only portal on their own website, that reads <i>only</i> their "
            "scoped data from the TrustNode cloud, with no exposure of the desktop edge code, "
            "the backend Python services, the OPC/MQTT runtimes, or the control-plane.", "ok",
        ),
        Spacer(1, 0.4 * cm),
        boxed(
            "<b>Non-goal:</b> distributing the desktop tray app, the .NET / Mosquitto "
            "sidecars, the FastAPI source, or any secret keys to the customer.",
            "danger",
        ),
        PageBreak(),
    ]

    # ── Section 1: Executive summary ──────────────────────────────────
    elements += [
        Paragraph("1. Executive summary", H1),
        Paragraph(
            "TrustNode ships in three layers: a Windows <b>desktop edge tray app</b> that runs "
            "inside the customer's plant, a <b>cloud control-plane + database</b> on the TrustNode "
            "VPS (Supabase Postgres), and a family of <b>web/lite views</b> that anyone with a "
            "browser can open. The packaging proposed here uses the same <i>web/lite</i> layer to "
            "create a per-customer, on-customer-domain, read-only portal that is operationally "
            "isolated from the proprietary edge and backend.",
            BODY,
        ),
        Paragraph(
            "Three properties of the existing architecture make this clean:",
            BODY,
        ),
        ListFlowable([
            ListItem(Paragraph(
                "The <b>cloud Lite</b> at <font name=\"Courier\">/lite/index.html</font> is a single "
                "self-contained HTML file that loads React + Recharts from a public CDN (esm.sh) and "
                "Supabase from the public JS SDK. <i>No build step is required to deploy it.</i>",
                BODY)),
            ListItem(Paragraph(
                "Tenant isolation lives in the <b>database</b> via Supabase Row-Level Security, NOT "
                "in the client. A leaked Lite HTML cannot read another customer's data even if the "
                "anonymous key is exposed.",
                BODY)),
            ListItem(Paragraph(
                "A small <font name=\"Courier\">config.json</font> sits next to the HTML and carries "
                "all per-customer customisation (label, tenant scope, enabled tabs, polling cadence). "
                "Customisation is configuration, not source code.",
                BODY)),
        ], bulletType="bullet", leftIndent=18),
        Paragraph(
            "Maintaining a customer-specific build therefore reduces to maintaining a small "
            "directory of static assets and a <font name=\"Courier\">config.json</font>. The "
            "underlying React bundle is reused across every customer; brand assets and configuration "
            "are the only forked surface.",
            BODY,
        ),
        Spacer(1, 0.3 * cm),
        boxed(
            "Recommended pattern: keep <b>one canonical Lite HTML</b> in this repo and produce per-customer "
            "folders that are <i>almost entirely brand assets + config</i>. Re-run a tiny copy "
            "script when the canonical Lite is rebuilt. No customer-side React build, no Vite, no Node.",
            "ok",
        ),
        PageBreak(),
    ]

    # ── Section 2: Repository inventory ───────────────────────────────
    elements += [
        Paragraph("2. Repository inventory", H1),
        Paragraph(
            "The full repo tree is reproduced below with each top-level folder's role. "
            "Sensitivity is rated relative to a customer-distribution risk model:",
            BODY,
        ),
        kv_table([
            ["Rating", "Meaning"],
            ["Low", "Public-safe. Brand assets, generic CSS, public CDN-based HTML, documented APIs."],
            ["Medium", "Internal but not catastrophic if exposed. Migration SQL, frontend source, docs."],
            ["High", "Must never reach the customer. Backend Python, edge tray app, secrets, .env, build scripts that embed VPS creds."],
        ]),
        Spacer(1, 0.3 * cm),
        Paragraph("2.1 Top-level layout", H2),
        kv_table([
            ["Folder", "Role"],
            ["backend/", "FastAPI edge service. Python app, runs inside the tray EXE AND on the cloud VPS."],
            ["frontend/", "React desktop UI source. Vite build → dist/ for tray and cloud portal."],
            ["desktop/", "Electron tray wrapper. main.js + NSIS installer hooks + Mosquitto/OPC sidecar staging."],
            ["web_cloud_readonly/", "Public-facing static deploy roots. lite/, portal/, developer-portal/, brand PNGs."],
            ["db/migrations/", "Supabase SQL migrations. Schema-of-record for the cloud database."],
            ["backend/sql/migrations/", "Older migration set for the VPS-owned schema (control_plane_core, telemetry_v1)."],
            ["docs/", "Architecture whitepapers, scalability reports, deployment diagrams, decks."],
            ["scripts/", "PowerShell/Python build + provisioning scripts (clientview build, customer provisioning, etc.)."],
            ["tests/, backend/tests/", "Smoke + unit tests. Never deployed."],
            ["client_page_tests/", "Documentation + experimental shells for the single-file client variants."],
            ["backups/, .runlogs/, runlogs/, .runtime-logs/, scripts/runtime-logs/", "Local-only operator scratch. Excluded from any customer deploy."],
        ]),
        Spacer(1, 0.3 * cm),
        Paragraph("2.2 Backend — backend/app/", H2),
        Paragraph(
            "Two sibling packages: <font name=\"Courier\">routers/</font> (HTTP endpoint groups) and "
            "<font name=\"Courier\">services/</font> (long-lived workers, persistence, gateway managers). "
            "Both are required for the FastAPI process and are bundled into the tray EXE via PyInstaller.",
            BODY,
        ),
        Paragraph("Routers", H3),
        file_table([
            ["backend/app/routers/auth.py", "Login + JWT issuance + temp passwords.", "High", "No"],
            ["backend/app/routers/control_plane.py", "Tenant/customer/edge admin, view-link mint, activation.", "High", "No"],
            ["backend/app/routers/plc.py", "Gateway runtime control, tag discovery, Modbus/AB/Snap7/OPC-UA browse.", "High", "No"],
            ["backend/app/routers/power.py", "Power-meter dashboard data + tariff math.", "High", "No"],
            ["backend/app/routers/reports.py", "PDF reporting, templates, schedules.", "High", "No"],
            ["backend/app/routers/notifications.py", "Email + SMTP test endpoints.", "High", "No"],
            ["backend/app/routers/app_store.py", "Local SQLite app-store get/put (config, historian, live, logs).", "High", "No"],
            ["backend/app/routers/database.py", "Local database management.", "High", "No"],
            ["backend/app/routers/historian_export.py", "XLSX export with formatting.", "High", "No"],
            ["backend/app/routers/connections.py", "OPC UA + MQTT toggle, runtime selector.", "High", "No"],
            ["backend/app/routers/customer_db.py", "Customer Postgres mode activation.", "High", "No"],
            ["backend/app/routers/lan_sharing.py", "LAN 0.0.0.0 toggle for in-plant Lite.", "High", "No"],
            ["backend/app/routers/lite_local.py", "View-link → session JWT exchange.", "High", "No"],
            ["backend/app/routers/cloud_live.py", "SSE stream of live data to cloud Lite.", "High", "No"],
            ["backend/app/routers/telemetry_v1.py", "Edge-side v1 ingest + query (device + user tokens).", "High", "No"],
            ["backend/app/routers/ui_source.py", "UI source config (local vs remote frontend).", "Medium", "No"],
            ["backend/app/routers/health.py", "Liveness probe.", "Low", "No"],
        ]),
        Paragraph("Services", H3),
        file_table([
            ["backend/app/services/app_store.py", "Local SQLite ORM-less layer (~7,300 LOC). Source of truth on the edge.", "High", "No"],
            ["backend/app/services/plc_manager.py", "Per-gateway worker. Allen-Bradley / Siemens / Modbus / OPC-UA loops.", "High", "No"],
            ["backend/app/services/power_manager.py", "Power-meter worker + tariff insights.", "High", "No"],
            ["backend/app/services/report_renderer.py", "PDF rendering via reportlab.", "High", "No"],
            ["backend/app/services/report_scheduler.py", "Cron-style timer + tag-trigger queue.", "High", "No"],
            ["backend/app/services/reports_store.py", "Report templates + history persistence.", "High", "No"],
            ["backend/app/services/control_plane_store.py", "SQLite-backed tenant/edge/license catalog.", "High", "No"],
            ["backend/app/services/control_plane_store_cloud.py", "Supabase-backed control plane (cloud variant).", "High", "No"],
            ["backend/app/services/cp_users_puller.py", "Pulls cp_users from cloud into local edge.", "High", "No"],
            ["backend/app/services/customer_sql.py", "Customer-Postgres engine pool + bootstrap.", "High", "No"],
            ["backend/app/services/sinks_sql.py", "Generic Postgres historian writer.", "High", "No"],
            ["backend/app/services/ingest_store.py", "Telemetry v1 ingest persistence.", "High", "No"],
            ["backend/app/services/telemetry_service.py", "Telemetry v1 service layer.", "High", "No"],
            ["backend/app/services/lite_user_mirror.py", "Mirrors local users into Supabase lite_profiles.", "High", "No"],
            ["backend/app/services/lite_report_poller.py", "Drains Supabase report queue on the cloud.", "High", "No"],
            ["backend/app/services/reports_cloud_uploader.py", "Uploads PDFs to Supabase Storage.", "High", "No"],
            ["backend/app/services/lan_socket.py", "Second in-process uvicorn on 0.0.0.0.", "High", "No"],
            ["backend/app/services/connections_publish.py", "Dispatches tag updates to OPC/MQTT runtimes.", "High", "No"],
            ["backend/app/services/opcua_server.py", "asyncua-based Python OPC UA server.", "High", "No"],
            ["backend/app/services/opcua_server_dotnet.py", "Manager around the OPC Foundation .NET sidecar.", "High", "No"],
            ["backend/app/services/mqtt_broker.py", "amqtt-based Python MQTT broker.", "High", "No"],
            ["backend/app/services/mqtt_broker_mosquitto.py", "Manager around the Mosquitto sidecar + paho publisher.", "High", "No"],
        ]),
        Paragraph("2.3 Frontend — frontend/", H2),
        file_table([
            ["frontend/src/App.jsx", "The full React UI (~33,000 LOC). Same bundle in tray + cloud portal + cloud Lite.", "Medium", "No (compiled output only)"],
            ["frontend/src/api.js", "Single source for /api/* + Supabase paths. Holds endpoint normalisers.", "Medium", "No"],
            ["frontend/src/components/Dashboard/*", "Dashboard editor + widgets + analytics.", "Medium", "No"],
            ["frontend/src/components/Login/Login.jsx + .css", "Login surface + theming.", "Medium", "No"],
            ["frontend/src/components/Icons/", "SVG icon library.", "Low", "Yes if embedded by Lite"],
            ["frontend/src/components/Reports/", "Report builder UI.", "Medium", "No"],
            ["frontend/src/styles*.css", "Base + portal + client + local theme sheets.", "Low", "Yes (when Lite uses them)"],
            ["frontend/vite.config.js", "Build modes: default, cloudro, clientview.", "Medium", "No"],
            ["frontend/.env.clientview", "Sets VITE_TRUSTNODE_CLIENT_VIEW=true. No secret here.", "Low", "No"],
            ["frontend/.env.cloudro", "Sets VITE_TRUSTNODE_READONLY=true. No secret here.", "Low", "No"],
            ["frontend/dist/", "Build output. Ships into tray EXE + /var/www/trustnode/.", "Low", "Indirect (via cloud)"],
            ["frontend/public/", "Static brand PNGs copied to dist.", "Low", "Yes"],
        ]),
        Paragraph("2.4 Desktop — desktop/", H2),
        file_table([
            ["desktop/main.js", "Electron tray entry. Spawns backend, manages LAN/firewall, sidecar lifecycle.", "High", "No"],
            ["desktop/preload.js", "IPC bridge for renderer.", "Medium", "No"],
            ["desktop/package.json", "Electron + electron-builder config. NSIS hooks. PerMachine install.", "Medium", "No"],
            ["desktop/build/installer.nsh", "Windows Firewall rule adds at install time.", "Low", "No"],
            ["desktop/assets/", "Tray icon, brand PNG.", "Low", "Yes (brand only)"],
            ["desktop/dist/", "Built NSIS + portable EXEs (~165 MB each).", "High", "No"],
        ]),
        Paragraph("2.5 Cloud static — web_cloud_readonly/", H2),
        Paragraph(
            "This is the only folder that public users reach directly. Everything here is "
            "intentionally distributable, with the caveat that <font name=\"Courier\">lite/config.json</font> "
            "may carry per-deployment branding and the Supabase anon key.",
            BODY,
        ),
        file_table([
            ["web_cloud_readonly/index.html", "Brand redirect into / (the React app).", "Low", "Optional"],
            ["web_cloud_readonly/lite/index.html", "Single-file React Lite via CDN imports (~4,600 LOC, 200 KB).", "Low", "Yes (canonical Lite)"],
            ["web_cloud_readonly/lite/config.json", "Per-deployment Supabase URL + anon key + tab toggles + branding label.", "Medium", "Yes (customised)"],
            ["web_cloud_readonly/lite/config.json.example", "Template for a fresh deployment.", "Low", "Yes"],
            ["web_cloud_readonly/lite/styles.css", "Lite-specific styling.", "Low", "Yes"],
            ["web_cloud_readonly/lite/manifest.json", "PWA manifest.", "Low", "Yes"],
            ["web_cloud_readonly/lite/sw.js", "Service worker (optional offline cache).", "Low", "Yes"],
            ["web_cloud_readonly/lite/trustnode_app_icon*.png", "App icons (PWA + apple-touch).", "Low", "Yes"],
            ["web_cloud_readonly/lite/trustnode_login_logo.png", "Brand logo on login.", "Low", "Yes"],
            ["web_cloud_readonly/lite/trustnode_logo.png", "Brand logo in header.", "Low", "Yes"],
            ["web_cloud_readonly/developer-portal/index.html", "Developer-only redirect to /developer-portal/.", "Low", "No"],
            ["web_cloud_readonly/portal/v1/index.html", "Bundler-loading skeleton; portal v1 deploys.", "Low", "No"],
            ["web_cloud_readonly/assets/", "Hashed JS/CSS used by /portal/.", "Medium", "No"],
            ["web_cloud_readonly/trustenode-004.png + trustnode_logo.png + login_background*.png", "Public brand assets.", "Low", "Yes"],
        ]),
        Paragraph("2.6 Database migrations — db/migrations/", H2),
        file_table([
            ["db/migrations/20260517_lite_realtime.sql", "Realtime publication for live_latest + dashboards.", "Medium", "No"],
            ["db/migrations/20260517_lite_rls.sql", "Row-Level Security policies on all data tables.", "Medium", "No"],
            ["db/migrations/20260517_dashboard_configurations.sql", "Dashboard configurations table + audit log.", "Medium", "No"],
            ["db/migrations/20260518_per_customer_tenant.sql", "Per-customer tenancy enforcement.", "Medium", "No"],
            ["db/migrations/20260518_*.sql", "Reports queue, historian indexes, etc.", "Medium", "No"],
            ["db/migrations/20260519_align_cp_schema_for_cloud_store.sql", "Schema alignment for cloud store.", "Medium", "No"],
            ["db/migrations/20260610_gateway_devices_mirror.sql", "Edge-to-cloud device mirror.", "Medium", "No"],
            ["db/migrations/20260611_lite_profile_auto_provision.sql", "Auto-create lite_profiles on first login.", "Medium", "No"],
            ["db/migrations/20260611_historian_tail_index.sql", "Tail-query index for historian.", "Medium", "No"],
            ["db/migrations/20260617_view_links_per_user.sql", "Per-user view-link tokens (user_id column).", "Medium", "No"],
        ]),
        PageBreak(),
    ]

    # ── Section 3: Data flow + customer-visible surface ───────────────
    elements += [
        Paragraph("3. Data flow and what the customer actually sees", H1),
        Paragraph(
            "Five layers, three roles, one canonical bundle. The customer-distributable surface "
            "is the green band only.",
            BODY,
        ),
        Paragraph("3.1 Layers", H2),
        kv_table([
            ["Layer", "Where it lives + who owns it"],
            ["1. PLC / meter", "Customer's plant network. TrustNode reads via gateway protocols."],
            ["2. Edge tray (Windows)", "Customer-installed EXE. SQLite locally; pushes to cloud."],
            ["3. Cloud control-plane + DB", "TrustNode VPS + Supabase. trustnode.lsapps.app."],
            ["4. Cloud Lite + Portal", "/lite/index.html (read-only Supabase) + / (full React portal)."],
            ["5. Customer-hosted Client View", "<b>(the new layer)</b> A copy of layer 4 on the customer's domain."],
        ]),
        Paragraph("3.2 Tenant scoping", H2),
        Paragraph(
            "TrustNode is multi-tenant. Every row in the cloud database carries a "
            "<font name=\"Courier\">tenant_id</font>. Each customer has their own tenant "
            "(see <font name=\"Courier\">20260518_per_customer_tenant.sql</font>). Row-Level "
            "Security policies (<font name=\"Courier\">20260517_lite_rls.sql</font>) enforce "
            "tenant separation at the database — even if a customer obtains another customer's "
            "anonymous key, Supabase will return zero rows because their user JWT does not "
            "satisfy the policy.",
            BODY,
        ),
        boxed(
            "<b>Key consequence:</b> a customer Client View hosted on the customer's own domain "
            "is just a static HTML + config.json. It cannot impersonate another tenant. The "
            "trust boundary is the Supabase JWT and the lite_profiles row, not the page.",
            "ok",
        ),
        Paragraph("3.3 What is hidden from the customer", H2),
        ListFlowable([
            ListItem(Paragraph("All Python code (<font name=\"Courier\">backend/</font>).", BODY)),
            ListItem(Paragraph("All React source (<font name=\"Courier\">frontend/src/</font>). The customer receives only the compiled, minified bundle hosted on the cloud.", BODY)),
            ListItem(Paragraph("Desktop tray (<font name=\"Courier\">desktop/</font>) — only the operator installs it on plant PCs.", BODY)),
            ListItem(Paragraph("Build scripts that embed VPS credentials (<font name=\"Courier\">scripts/_*</font>, .env files).", BODY)),
            ListItem(Paragraph("Migrations + smoke tests — internal engineering only.", BODY)),
            ListItem(Paragraph("Whitepapers, decks, internal docs in <font name=\"Courier\">docs/</font>.", BODY)),
            ListItem(Paragraph("Native sidecars (<font name=\"Courier\">backend/sidecars/</font>) and the C# sidecar source (<font name=\"Courier\">backend/sidecars/opcua-cs/</font>).", BODY)),
        ], bulletType="bullet", leftIndent=18),
        Paragraph("3.4 What is exposed to the customer (the green band)", H2),
        ListFlowable([
            ListItem(Paragraph("One HTML file (the cloud Lite).", BODY)),
            ListItem(Paragraph("One <font name=\"Courier\">config.json</font> with their tenant_id, customer label, tab toggles, polling intervals.", BODY)),
            ListItem(Paragraph("Brand PNGs (logo, login background, app icons).", BODY)),
            ListItem(Paragraph("CSS + service worker + PWA manifest.", BODY)),
            ListItem(Paragraph("Public Supabase URL + <i>anon</i> public key. Security comes from RLS, not key secrecy.", BODY)),
        ], bulletType="bullet", leftIndent=18),
        PageBreak(),
    ]

    # ── Section 4: Customer folder design ─────────────────────────────
    elements += [
        Paragraph("4. The customer folder — recommended layout", H1),
        Paragraph(
            "Below is a maintainable folder layout intended to be checked into a small per-customer "
            "git repository (or shipped to the customer's web team as a zip). It is small enough to "
            "audit by eye, contains zero proprietary code, and is updated by re-running a single "
            "synchronisation script when the canonical Lite is rebuilt.",
            BODY,
        ),
        Spacer(1, 0.2 * cm),
        Paragraph(
            "<font name=\"Courier\" size=\"9\">"
            "trustnode-customer-&lt;NAME&gt;/<br/>"
            "├── README.md                ← how to update + how to host<br/>"
            "├── public/                  ← drop-in for any static web host<br/>"
            "│   ├── index.html           ← copy of /lite/index.html<br/>"
            "│   ├── styles.css           ← copy of /lite/styles.css<br/>"
            "│   ├── manifest.json        ← copy of /lite/manifest.json<br/>"
            "│   ├── sw.js                ← copy of /lite/sw.js<br/>"
            "│   ├── config.json          ← <b>CUSTOMER-SPECIFIC</b><br/>"
            "│   ├── trustnode_app_icon.png<br/>"
            "│   ├── trustnode_app_icon_180.png<br/>"
            "│   ├── trustnode_login_logo.png   ← <b>customer brand override</b><br/>"
            "│   └── trustnode_logo.png         ← <b>customer brand override</b><br/>"
            "├── source/                  ← upstream reference (do not edit)<br/>"
            "│   ├── lite_index.html.upstream.txt  ← hash of last sync<br/>"
            "│   └── lite_styles.css.upstream.txt<br/>"
            "└── sync.ps1                 ← copies the canonical Lite into public/<br/>"
            "</font>",
            BODY,
        ),
        Paragraph(
            "<b>Operational philosophy:</b> the <font name=\"Courier\">public/</font> folder is what "
            "the customer hosts. <font name=\"Courier\">config.json</font> and the brand PNGs are the "
            "ONLY files the integrator edits per customer. Everything else comes from the canonical "
            "Lite via <font name=\"Courier\">sync.ps1</font>.",
            BODY,
        ),
        Paragraph("4.1 config.json fields and how to fill them", H2),
        kv_table([
            ["Field", "Value + notes"],
            ["supabase_url", "https://&lt;PROJECT-REF&gt;.supabase.co · same for all customers · safe to commit"],
            ["supabase_anon_key", "Public anon key from the Supabase project · safe to commit · security comes from RLS"],
            ["customer_label", "Shown in the Lite header. e.g. \"Acme Bottling Plant\"."],
            ["tenant_id", "The customer's tenant in cp_customers. e.g. tenant-acme. Informational only."],
            ["show_dashboard_tab", "true / false"],
            ["show_alarms_tab", "true / false"],
            ["show_power_tab", "true / false"],
            ["live_poll_ms", "Default 2000. Lower = livelier, more bandwidth."],
            ["history_default_limit", "Default 300 rows."],
            ["default_chart_range_minutes", "Default 60."],
        ]),
        Paragraph("4.2 sync.ps1 — the recommended sync recipe", H2),
        Paragraph(
            "The script keeps the canonical cloud Lite as the source of truth. When the platform "
            "team rebuilds the Lite (because of a new widget type, a chart fix, a brand refresh), "
            "the integrator runs <font name=\"Courier\">sync.ps1</font> on every customer folder to "
            "pull in the latest. Brand assets and <font name=\"Courier\">config.json</font> are left "
            "untouched by the sync.",
            BODY,
        ),
        Paragraph(
            "<font name=\"Courier\" size=\"8\">"
            "param([string]$CanonicalRoot = \"&lt;path-to-Trustnode_edge_app&gt;\\web_cloud_readonly\\lite\")<br/>"
            "$dest = Join-Path $PSScriptRoot \"public\"<br/>"
            "$keep = @(\"config.json\", \"trustnode_login_logo.png\", \"trustnode_logo.png\")<br/>"
            "Get-ChildItem $CanonicalRoot -File | ForEach-Object {<br/>"
            "&nbsp;&nbsp;if ($keep -contains $_.Name) { return }<br/>"
            "&nbsp;&nbsp;Copy-Item $_.FullName -Destination (Join-Path $dest $_.Name) -Force<br/>"
            "}<br/>"
            "Write-Host \"Sync complete. Review public/config.json before deploy.\"<br/>"
            "</font>",
            BODY,
        ),
        Paragraph("4.3 Hosting", H2),
        ListFlowable([
            ListItem(Paragraph(
                "<b>Static host</b> (Netlify, Vercel, Cloudflare Pages, customer's own nginx): "
                "upload <font name=\"Courier\">public/</font>. No build step.",
                BODY)),
            ListItem(Paragraph(
                "<b>Customer's website</b> (e.g. customer.com/portal/): drop the contents of "
                "<font name=\"Courier\">public/</font> into <font name=\"Courier\">/portal/</font> under their "
                "document root. Adjust the manifest's <font name=\"Courier\">scope</font> + <font name=\"Courier\">start_url</font> "
                "if not at the host root.",
                BODY)),
            ListItem(Paragraph(
                "<b>CDN</b>: upload to S3 / R2 / GCS bucket with public read. Configure CORS only "
                "if the canonical Supabase URL refuses requests from the new origin (it normally accepts any).",
                BODY)),
        ], bulletType="bullet", leftIndent=18),
        Paragraph("4.4 Brand customisation", H2),
        Paragraph(
            "Replace the two PNGs (<font name=\"Courier\">trustnode_login_logo.png</font>, "
            "<font name=\"Courier\">trustnode_logo.png</font>) with co-branded artwork. Optionally "
            "replace the app icon PNGs to change the install-to-home-screen badge. Update "
            "<font name=\"Courier\">customer_label</font> in <font name=\"Courier\">config.json</font>. "
            "Avoid editing <font name=\"Courier\">index.html</font> directly — any change is overwritten "
            "by the next <font name=\"Courier\">sync.ps1</font>.",
            BODY,
        ),
        Paragraph(
            "If the customer requires a colour palette override that isn't in config.json today, "
            "the cleanest path is to add a few CSS variables to <font name=\"Courier\">public/brand.css</font> "
            "and include it after <font name=\"Courier\">styles.css</font>. Long-term, those variables should "
            "be promoted into <font name=\"Courier\">config.json</font> so customisation stays declarative.",
            BODY,
        ),
        PageBreak(),
    ]

    # ── Section 5: Sensitive surface + secret handling ────────────────
    elements += [
        Paragraph("5. Sensitive surface + secrets to never ship", H1),
        Paragraph(
            "Treat the customer folder as if it will be publicly inspected. Everything below "
            "must stay behind the platform boundary.",
            BODY,
        ),
        kv_table([
            ["Asset", "Where it lives + why it must not ship"],
            [".env / .env files", "Cloud DB password, Supabase service key, SMTP creds, VPS root password. NEVER commit anywhere customer-readable."],
            ["TRUSTNODE_SUPABASE_SERVICE_KEY", "Bypasses RLS. Whoever has this can read every customer's data."],
            ["VPS_HOST / VPS_USER / VPS_PASSWORD", "Direct shell access to the production VPS."],
            ["TRUSTNODE_CLOUD_DB_PASSWORD", "Direct Postgres write access."],
            ["backend/*.py", "Tenant logic, control-plane logic, billing/license logic."],
            ["frontend/src/*.jsx", "Trade-secret in the layout/widget editor + designer."],
            ["scripts/_*.py + apply-*.ps1", "Operational scripts that embed cloud creds at runtime."],
            ["backend/sidecars/opcua-cs/", "C# source for the OPC UA sidecar. Compiled binary is OK to ship inside the EXE; source is not."],
            ["docs/TrustNode_Security_*", "Internal security whitepapers."],
            ["desktop/dist/", "Built EXEs. Customer-facing only via the official download URL."],
        ]),
        Paragraph("5.1 Threat model for the customer Client View", H2),
        kv_table([
            ["Threat", "Mitigation"],
            ["Customer modifies the HTML to query another tenant", "Supabase RLS rejects the query — no rows returned."],
            ["Anonymous key leaked", "Public by design. Same outcome — RLS denies cross-tenant reads."],
            ["Customer hosts the HTML on a malicious origin", "User JWT still required; no cross-origin credential leak as no cookies are used."],
            ["Customer attempts to write data", "Lite is read-only; write paths are not implemented; RLS denies writes from anon."],
            ["Customer attempts to call backend /api/* directly", "Backend rejects without a valid Bearer JWT (cp_users authoritative)."],
            ["Customer extracts edge source from the EXE", "PyInstaller bytecode is not protected from reverse engineering. Defence: avoid shipping the EXE to customers — only operators install it."],
        ]),
        Paragraph("5.2 Secret rotation hooks", H2),
        Paragraph(
            "If a customer-hosted Client View is ever suspected compromised, the response is to "
            "<b>rotate the customer's user passwords</b> (or revoke their per-user view-link tokens "
            "via Control Plane → Users → Lite Access → Rotate). Neither action requires touching the "
            "Lite file. The anon key itself does not need rotation because RLS makes it impotent.",
            BODY,
        ),
        Paragraph(
            "Service-role keys and DB passwords are unrelated to Lite and should be rotated only on "
            "their own schedule (e.g., annually or on staff change).",
            BODY,
        ),
        PageBreak(),
    ]

    # ── Section 6: Maintenance + release flow ─────────────────────────
    elements += [
        Paragraph("6. Maintenance + release flow", H1),
        Paragraph(
            "Three release tracks. Customer folders depend on track 2 only.",
            BODY,
        ),
        kv_table([
            ["Track", "Cadence + what changes"],
            ["1. Edge tray EXE", "On-demand. desktop/dist/ rebuilt + signed + shipped to operators. Customer folders unaffected."],
            ["2. Canonical Lite (web_cloud_readonly/lite/)", "When a customer-visible bug/feature lands. Re-run sync.ps1 in every customer folder."],
            ["3. Cloud control-plane (backend on VPS)", "On commit to main + restart. Customer folders pick up the new data shapes automatically because Lite reads from the new schema."],
        ]),
        Paragraph("6.1 Customer-folder update checklist", H2),
        ListFlowable([
            ListItem(Paragraph("Pull the latest <font name=\"Courier\">main</font> in the TrustNode repo.", BODY)),
            ListItem(Paragraph("In each <font name=\"Courier\">trustnode-customer-&lt;NAME&gt;/</font> repo, run <font name=\"Courier\">.\\sync.ps1</font>.", BODY)),
            ListItem(Paragraph("Inspect <font name=\"Courier\">public/config.json</font> — it must be untouched.", BODY)),
            ListItem(Paragraph("Verify brand PNGs still present.", BODY)),
            ListItem(Paragraph("Commit + push the customer repo.", BODY)),
            ListItem(Paragraph("If hosted on Netlify/Vercel, deploy auto-fires from the push.", BODY)),
            ListItem(Paragraph("Smoke: open the URL, log in with one cloud user, see expected dashboards.", BODY)),
        ], bulletType="bullet", leftIndent=18),
        Paragraph("6.2 Schema breaks", H2),
        Paragraph(
            "When a Supabase migration changes a Lite-visible column or table, both the canonical "
            "Lite and any customer folder must be re-synced. The current architecture loads React + "
            "Recharts from esm.sh, so a bumped column rarely requires a Lite change — only a chart "
            "rename or a removed table does. Apply migrations to Supabase before customers reload "
            "their Lite to avoid a brief schema mismatch.",
            BODY,
        ),
        Paragraph("6.3 Adding a new customer", H2),
        ListFlowable([
            ListItem(Paragraph("In cloud Control Plane → Customers → New: create the customer, get its tenant_id.", BODY)),
            ListItem(Paragraph("Create the customer's admin user (Control Plane → Users → New).", BODY)),
            ListItem(Paragraph("Clone the customer-folder template: <font name=\"Courier\">cp -r trustnode-customer-template trustnode-customer-acme</font>.", BODY)),
            ListItem(Paragraph("Edit <font name=\"Courier\">public/config.json</font>: tenant_id + customer_label.", BODY)),
            ListItem(Paragraph("Drop in customer-branded PNGs (optional).", BODY)),
            ListItem(Paragraph("Run <font name=\"Courier\">.\\sync.ps1</font> to pull the current canonical Lite.", BODY)),
            ListItem(Paragraph("Host on the customer's chosen origin.", BODY)),
        ], bulletType="bullet", leftIndent=18),
        PageBreak(),
    ]

    # ── Section 7: Open considerations ────────────────────────────────
    elements += [
        Paragraph("7. Open considerations + recommendations", H1),
        Paragraph("7.1 Lite versioning", H2),
        Paragraph(
            "The cloud Lite currently has a CSS cache-buster (<font name=\"Courier\">?v=20260612-1700</font>) "
            "but no version metadata in the HTML itself. Recommend adding a "
            "<font name=\"Courier\">&lt;meta name=\"tn-lite-version\"&gt;</font> tag and printing the build hash "
            "in the footer, so an integrator can verify which build a given customer is running.",
            BODY,
        ),
        Paragraph("7.2 Brand-variable promotion", H2),
        Paragraph(
            "Today, palette overrides need a side-file <font name=\"Courier\">brand.css</font>. "
            "Recommend promoting brand colours (--brand, --teal, --bg, --card) into config.json so "
            "customer customisation stays JSON-only and a single integrator can configure dozens of "
            "tenants without touching CSS.",
            BODY,
        ),
        Paragraph("7.3 Multi-domain Supabase Auth", H2),
        Paragraph(
            "When a customer hosts Lite on their own domain, Supabase Auth still works (no cookies, "
            "only the user JWT in localStorage). Confirm the redirect URLs in Supabase Auth settings "
            "include the customer's domain if magic-link email login is enabled.",
            BODY,
        ),
        Paragraph("7.4 Operational hygiene", H2),
        ListFlowable([
            ListItem(Paragraph("Keep customer folders in a <i>separate</i> git org so accidental pushes never leak into the main TrustNode repo.", BODY)),
            ListItem(Paragraph("Never share VPS credentials with the customer or their integrator.", BODY)),
            ListItem(Paragraph("Disable Supabase service-role key usage from any browser-facing code.", BODY)),
            ListItem(Paragraph("Log every customer-folder sync to a CHANGELOG.md inside the customer repo for traceability.", BODY)),
        ], bulletType="bullet", leftIndent=18),
        PageBreak(),
    ]

    # ── Section 8: One-page reference appendix ───────────────────────
    elements += [
        Paragraph("Appendix A — what ships where", H1),
        kv_table([
            ["Audience", "Receives"],
            ["Operator (plant PC)", "TrustNode-Setup.exe (installer EXE) + activation code"],
            ["Cloud team", "Full repository (git clone)"],
            ["Customer's web team", "trustnode-customer-&lt;NAME&gt;/ folder only (public/ + sync.ps1 + README)"],
            ["End user (browser)", "The hosted Lite URL. No downloads. No installation."],
            ["TrustNode internal", "docs/, scripts/, .env files, decks"],
        ]),
        Paragraph("Appendix B — minimum customer folder file list", H1),
        Paragraph(
            "Smallest viable folder for a working customer-branded portal. Total size: about 700 KB.",
            BODY,
        ),
        kv_table([
            ["File", "Origin"],
            ["public/index.html", "Copy of web_cloud_readonly/lite/index.html"],
            ["public/styles.css", "Copy of web_cloud_readonly/lite/styles.css"],
            ["public/manifest.json", "Copy of web_cloud_readonly/lite/manifest.json"],
            ["public/sw.js", "Copy of web_cloud_readonly/lite/sw.js"],
            ["public/config.json", "From web_cloud_readonly/lite/config.json.example, filled in for the customer"],
            ["public/trustnode_app_icon.png", "Customer brand (or canonical fallback)"],
            ["public/trustnode_app_icon_180.png", "Customer brand (or canonical fallback)"],
            ["public/trustnode_login_logo.png", "Customer brand"],
            ["public/trustnode_logo.png", "Customer brand"],
            ["README.md", "Operator-written"],
            ["sync.ps1", "Operator-written (see § 4.2)"],
        ]),
        Paragraph("Appendix C — what NEVER goes in a customer folder", H1),
        kv_table([
            ["Item", "Reason"],
            [".env / .env.* / .env.example", "Carries secrets even in 'example' form (project ref)"],
            ["backend/", "Trade secret, license logic, control-plane logic"],
            ["frontend/src/", "Source for the React app — only the compiled cloud bundle is publicly served"],
            ["desktop/", "Tray app implementation"],
            ["backend/sidecars/opcua-cs/", "C# source for OPC UA sidecar"],
            ["scripts/", "Operational scripts"],
            ["db/migrations/", "Internal schema design"],
            ["docs/", "Decks + whitepapers + security architecture details"],
            ["tests/, backend/tests/", "Internal test infrastructure"],
            ["backups/, *.log", "Audit + scratch data"],
        ]),
        Spacer(1, 0.5 * cm),
        boxed(
            "Report compiled by TrustNode engineering. For questions about packaging a specific "
            "customer, open an internal ticket referencing this report and quote the customer's "
            "tenant_id.",
            "note",
        ),
    ]

    doc.build(elements, onFirstPage=page_decoration, onLaterPages=page_decoration)
    print(f"PDF written to: {OUT}")
    print(f"Size: {OUT.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
