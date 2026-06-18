"""Generate customer-facing security + onboarding PDFs.

Two outputs:
  docs/customer_security_note_2026-06-18.pdf     — for the customer IT auditor
  docs/customer_onboarding_2026-06-18.pdf        — for the plant operator

Run once with:
    python docs/customer_security_note.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    ListFlowable, ListItem,
)

HERE = Path(__file__).resolve().parent

BRAND_NAVY = colors.HexColor("#1f3a5f")
BRAND_TEAL = colors.HexColor("#0e8479")
INK = colors.HexColor("#0f172a")
INK_SOFT = colors.HexColor("#64748b")
OK_BG = colors.HexColor("#ecfdf5")
OK_BORDER = colors.HexColor("#059669")
WARN_BG = colors.HexColor("#fff7e6")
WARN_BORDER = colors.HexColor("#d97706")
DANGER_BG = colors.HexColor("#fef2f2")
DANGER_BORDER = colors.HexColor("#dc2626")

styles = getSampleStyleSheet()

H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontName="Helvetica-Bold",
                    fontSize=22, leading=26, textColor=BRAND_NAVY, spaceAfter=10)
H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontName="Helvetica-Bold",
                    fontSize=15, leading=20, textColor=BRAND_NAVY,
                    spaceAfter=8, spaceBefore=14)
H3 = ParagraphStyle("H3", parent=styles["Heading3"], fontName="Helvetica-Bold",
                    fontSize=12, leading=16, textColor=BRAND_TEAL,
                    spaceAfter=4, spaceBefore=10)
BODY = ParagraphStyle("Body", fontName="Helvetica", fontSize=10, leading=14,
                      textColor=INK, alignment=TA_LEFT, spaceAfter=6)
SUB = ParagraphStyle("Sub", fontName="Helvetica-Oblique", fontSize=9,
                     leading=12, textColor=INK_SOFT, spaceAfter=8)


def boxed(text: str, kind: str = "note"):
    bg, border = colors.HexColor("#f6f8fa"), colors.HexColor("#e5e7eb")
    if kind == "ok": bg, border = OK_BG, OK_BORDER
    elif kind == "warn": bg, border = WARN_BG, WARN_BORDER
    elif kind == "danger": bg, border = DANGER_BG, DANGER_BORDER
    s = ParagraphStyle("Boxed", parent=BODY, backColor=bg, borderColor=border,
                       borderWidth=0.8, borderPadding=8, leftIndent=8, rightIndent=8)
    return Paragraph(text, s)


def kv_table(rows, col_widths=None):
    if col_widths is None:
        col_widths = [5.5 * cm, 11.5 * cm]
    t = Table(rows, colWidths=col_widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_NAVY),
        ("FONT", (0, 1), (0, -1), "Helvetica-Bold", 9),
    ]))
    return t


def header(canvas, doc, title):
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(BRAND_NAVY)
    canvas.rect(0, h - 1.2 * cm, w, 1.2 * cm, stroke=0, fill=1)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(1.6 * cm, h - 0.78 * cm, title)
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(w - 1.6 * cm, h - 0.78 * cm, "2026-06-18")
    canvas.setFillColor(INK_SOFT)
    canvas.setFont("Helvetica", 8.5)
    canvas.drawString(1.6 * cm, 1.0 * cm, "TrustNode Edge")
    canvas.drawRightString(w - 1.6 * cm, 1.0 * cm, f"Page {doc.page}")
    canvas.restoreState()


# ── Security note (for the customer's IT auditor) ──────────────────────

def build_security_note():
    out = HERE / "customer_security_note_2026-06-18.pdf"
    title = "TrustNode Edge — Security Architecture Note"
    def deco(c, d): return header(c, d, title)
    doc = SimpleDocTemplate(str(out), pagesize=A4,
                            leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=1.7 * cm, bottomMargin=1.5 * cm,
                            title=title, author="TrustNode")
    e = []
    e += [
        Spacer(1, 4.0 * cm),
        Paragraph(title, H1),
        Paragraph("Intended audience: customer IT / OT security reviewer", SUB),
        Spacer(1, 0.4 * cm),
        Paragraph(
            "This document covers the data flow, attack surface, and "
            "controls that the TrustNode Edge agent applies on a customer "
            "site. It is intended for an IT security reviewer evaluating "
            "the agent for production deployment.",
            BODY,
        ),
        Spacer(1, 0.3 * cm),
        boxed(
            "TrustNode Edge is an on-premise agent that polls industrial "
            "PLCs and power meters, stores the data locally in SQLite, and "
            "optionally mirrors it to a customer-tenant Supabase Postgres. "
            "No customer plant data is ever sent to a TrustNode-owned "
            "database; the mirror target is the customer's own cloud "
            "tenancy.",
            "ok",
        ),
        PageBreak(),
    ]

    e += [
        Paragraph("1. Network exposure", H1),
        kv_table([
            ["Listener", "Default port + purpose"],
            ["Backend HTTP", "127.0.0.1:8000 — local-only by default. Tray UI and Lite/Client View talk to it over loopback."],
            ["LAN HTTP (optional)", "0.0.0.0:8088-8092 — only when admin enables LAN sharing. Token + login gated. Per-user variant gate (Full / Lite / Client)."],
            ["OPC UA server (optional)", "0.0.0.0:4840 — only when admin enables the OPC UA service. Anonymous or username/password."],
            ["MQTT broker (optional)", "0.0.0.0:1883 — only when admin enables Mosquitto/amqtt. Anonymous or username/password."],
            ["Outbound to TrustNode cloud", "HTTPS to trustnode.lsapps.app — control-plane only (license check, user sync). No plant data."],
            ["Outbound to customer Supabase", "HTTPS (port 443) + Postgres (port 5432/6543). Customer-owned project; only their tenant_id."],
        ]),
        Paragraph("2. Authentication and authorisation", H1),
        kv_table([
            ["Identity", "Mechanism"],
            ["Tray app user", "Username + password against the local cp_users table. JWT issued by /api/auth/login."],
            ["LAN web viewers", "Same cp_users login OR a view-link token issued by the admin per user."],
            ["Per-variant access", "access_full / access_lite / access_client flags. Backend enforces via /api/lite-local/check-access on every variant entry."],
            ["Tray menu (LAN sharing toggle)", "Admin/super role only. Server-verified via /api/auth/me on every right-click — renderer cannot spoof."],
            ["Cloud Lite viewers", "Supabase Auth (email + password). Row-Level Security restricts every read to the user's tenant_id."],
            ["Service-to-service", "ED25519-signed license payload (private key never leaves the dev portal VPS; public key bundled in the tray EXE)."],
        ]),
        Paragraph("3. Data protection at rest", H1),
        kv_table([
            ["Data", "Location + controls"],
            ["SQLite buffer DB", "C:\\ProgramData\\TrustNode\\edge\\trustnode_app_store.db. NTFS ACL: SYSTEM + Administrators full control; Authenticated Users read-only. Regular Windows users cannot edit."],
            ["License + module flags", "Same SQLite. License signature (ED25519) detects edits — flipping a module flag without re-signing invalidates the signature and the UI locks all premium modules + writes a tamper alert to app_logs."],
            ["Generated reports", "Customer-configurable directory (default Documents\\TrustNode\\reports). Operator can redirect to a network share via the Directories page."],
            ["Customer plant data on cloud", "Customer's own Supabase project. Row-Level Security policies enforce tenant separation."],
        ]),
        Paragraph("4. Tamper detection", H1),
        ListFlowable([
            ListItem(Paragraph(
                "<b>License tamper alert</b> — every license-check verifies the ED25519 signature. "
                "Invalid signature writes an ERROR row to app_logs (category=license_tamper) AND surfaces a red banner in the UI.",
                BODY)),
            ListItem(Paragraph(
                "<b>Backend write veto</b> — historian writes are refused if the license is expired with no active trial OR the signature is invalid (Phase 3b).",
                BODY)),
            ListItem(Paragraph(
                "<b>Tray role spoofing</b> — the tray re-verifies the renderer's claimed user role against /api/auth/me on every menu open (Phase 3a). LAN sharing toggle requires server-confirmed admin/super.",
                BODY)),
            ListItem(Paragraph(
                "<b>Crash reporting</b> — if TRUSTNODE_SENTRY_DSN is set on the customer machine, uncaught Python/Node exceptions are reported to Sentry. Opt-in only; no PII sent by default.",
                BODY)),
        ], bulletType="bullet", leftIndent=18),
        Paragraph("5. Source code protection", H1),
        Paragraph(
            "The backend ships as a PyInstaller bundle. The output contains "
            "only .pyd (compiled extensions) and bytecode-in-archive (.pyc "
            "inside base_library.zip and the app.pkg archive). No .py source "
            "files are present on the customer install. Bytecode is "
            "stripped of docstrings and asserts (PyInstaller optimize=2).",
            BODY,
        ),
        boxed(
            "Honest disclosure: PyInstaller bytecode can be partially recovered with publicly-available "
            "tools (uncompyle6, decompyle3). A determined attacker with administrator rights and a few hours can "
            "reconstruct large portions of the backend logic. The TrustNode license signature (ED25519 with a "
            "VPS-side private key) stops the attacker from MONETISING the recovered source — they cannot forge "
            "a license to unlock paid modules. For deployments requiring stronger code protection, a future "
            "release can compile sensitive modules via Cython AOT (no additional licensing cost). "
            "See backend/app/services/COMPILE_SENSITIVE.md.",
            "warn",
        ),
        Paragraph("6. Anonymous keys and public secrets", H1),
        Paragraph(
            "The cloud Lite (and customer-hosted Lite if used) carries the "
            "Supabase project URL and the anonymous PUBLIC key. This is by "
            "design — Supabase considers the anon key public and enforces "
            "all isolation at the database via Row-Level Security policies. "
            "The customer's auditor may flag this as 'secret in client'; the "
            "correct response is that the anon key alone CANNOT read a "
            "row outside the signed-in user's tenant.",
            BODY,
        ),
        boxed(
            "The customer-side anon key is functionally similar to a public API endpoint URL — "
            "knowing it doesn't grant access. The SERVICE ROLE key (which bypasses RLS) is NEVER bundled "
            "in any client and lives only on the VPS in an env var.",
            "ok",
        ),
        PageBreak(),
        Paragraph("Appendix — Disclosed limitations", H1),
        kv_table([
            ["Item", "Status"],
            ["Code signing certificate (Authenticode)", "Not yet — Windows SmartScreen will show 'Unknown publisher'. Recommend $200/yr OV cert before wide rollout."],
            ["Crash reporting (Sentry)", "Available (Phase 3d) — opt-in via TRUSTNODE_SENTRY_DSN env var."],
            ["Source obfuscation beyond PyInstaller", "Documented path (Cython AOT) but not enabled by default. See COMPILE_SENSITIVE.md."],
            ["Status page", "Not yet."],
            ["Automated security scanning of dependencies", "Not yet."],
        ]),
    ]
    doc.build(e, onFirstPage=deco, onLaterPages=deco)
    print(f"wrote {out}")


# ── Onboarding PDF (for the plant operator) ────────────────────────────

def build_onboarding():
    out = HERE / "customer_onboarding_2026-06-18.pdf"
    title = "TrustNode Edge — Onboarding"
    def deco(c, d): return header(c, d, title)
    doc = SimpleDocTemplate(str(out), pagesize=A4,
                            leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=1.7 * cm, bottomMargin=1.5 * cm,
                            title=title, author="TrustNode")
    e = []
    e += [
        Spacer(1, 4.0 * cm),
        Paragraph(title, H1),
        Paragraph("Intended audience: plant operator setting up TrustNode on a Windows machine", SUB),
        Spacer(1, 0.4 * cm),
        PageBreak(),
        Paragraph("1. What you need before you start", H1),
        ListFlowable([
            ListItem(Paragraph("A Windows 10 or 11 machine on the plant network with administrator rights.", BODY)),
            ListItem(Paragraph("An activation code from your TrustNode contact.", BODY)),
            ListItem(Paragraph("The IPs and ports of any PLCs / power meters you plan to read.", BODY)),
            ListItem(Paragraph("Internet access for the initial license activation (the agent can run offline afterwards for up to 30 days).", BODY)),
        ], bulletType="bullet", leftIndent=18),

        Paragraph("2. Install", H1),
        Paragraph("Run <b>TrustNode-Setup-0.1.0.exe</b>. The installer:", BODY),
        ListFlowable([
            ListItem(Paragraph("Asks for administrator rights (UAC prompt).", BODY)),
            ListItem(Paragraph("Creates C:\\ProgramData\\TrustNode\\edge with restrictive permissions.", BODY)),
            ListItem(Paragraph("Adds Windows Firewall rules for the backend + optional OPC UA + MQTT sidecars.", BODY)),
            ListItem(Paragraph("Installs a Start Menu + Desktop shortcut.", BODY)),
        ], bulletType="bullet", leftIndent=18),

        Paragraph("3. First launch — activate", H1),
        ListFlowable([
            ListItem(Paragraph("Double-click the TrustNode icon. The tray icon appears in the system tray.", BODY)),
            ListItem(Paragraph("Open the main window. Sign in as <code>admin</code> / <code>admin</code> (you will be asked to change this password immediately).", BODY)),
            ListItem(Paragraph("Go to <b>Settings → Edge</b> and paste your activation code. Click Activate.", BODY)),
            ListItem(Paragraph("The license loads. The Edge page shows your customer name + the list of licensed modules.", BODY)),
        ], bulletType="bullet", leftIndent=18),

        Paragraph("4. Add a gateway / PLC / meter", H1),
        ListFlowable([
            ListItem(Paragraph("Open <b>Gateway Configuration</b>. Click + Add.", BODY)),
            ListItem(Paragraph("Pick the protocol (Allen-Bradley / Siemens / Modbus / OPC UA / Power meter).", BODY)),
            ListItem(Paragraph("Enter IP + port. Click Test connection.", BODY)),
            ListItem(Paragraph("Go to <b>Tags</b> and pick which tags to publish to the dashboard and/or the OPC UA / MQTT outbound feeds.", BODY)),
            ListItem(Paragraph("Open <b>Dashboard</b> and drag widgets onto the canvas.", BODY)),
        ], bulletType="bullet", leftIndent=18),

        Paragraph("5. Sharing access with the team", H1),
        ListFlowable([
            ListItem(Paragraph("Go to <b>Users and Access Control</b>. Click + Add user.", BODY)),
            ListItem(Paragraph("Pick which sections they can see (Module visibility) and which LAN web views they can open (LAN Web Access: Full / Lite / Client View).", BODY)),
            ListItem(Paragraph("Set a starting password — they will be forced to change it on first login.", BODY)),
            ListItem(Paragraph("If you want to give them a one-click URL: from the Users table → Lite Access column → click Full, Lite, or Client View to copy a token URL to clipboard. Send it to them by email or chat.", BODY)),
        ], bulletType="bullet", leftIndent=18),

        Paragraph("6. Customising save locations", H1),
        Paragraph(
            "Go to <b>Settings → Directories</b> to redirect reports, "
            "exports, logs, and backups to a network share or any other "
            "folder. Each row has an Open button (reveals in Explorer) "
            "and a Pick button (native folder dialog). Save changes "
            "applies the new paths immediately.",
            BODY,
        ),

        Paragraph("7. Troubleshooting", H1),
        kv_table([
            ["Symptom", "Action"],
            ["Tray icon never appears", "Check that the TrustNode-Backend service is running (services.msc). If it's stopped: right-click → Start."],
            ["License is locked / red banner", "Your license file may have been modified or expired. Re-activate from Settings → Edge using a fresh activation code from your TrustNode contact."],
            ["LAN URL not reachable from another computer", "From Settings → Connections → LAN Sharing, confirm the status pill says Running. If it says 'Bind failed', another process is on the same port — check the endpoints list and try a different port (8089 / 8090)."],
            ["A LAN user gets 'no access to <variant>'", "Edit their user record and tick access_full / access_lite / access_client matching the variant they need."],
        ]),

        Paragraph("8. Getting help", H1),
        Paragraph(
            "Open a support ticket with your TrustNode contact. Attach:",
            BODY,
        ),
        ListFlowable([
            ListItem(Paragraph("The boot log: <code>%LOCALAPPDATA%\\TrustNode\\boot-error.log</code>", BODY)),
            ListItem(Paragraph("The last 200 lines of app_logs from <b>Data History → Logs</b>", BODY)),
            ListItem(Paragraph("A screenshot of <b>Settings → Edge</b> showing the license status", BODY)),
        ], bulletType="bullet", leftIndent=18),
    ]
    doc.build(e, onFirstPage=deco, onLaterPages=deco)
    print(f"wrote {out}")


def main():
    build_security_note()
    build_onboarding()


if __name__ == "__main__":
    main()
