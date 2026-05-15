"""Render the five architecture diagrams used by the security whitepaper.

Output PNGs land in docs/diagrams/ at 1600 x 900 (16:9), suitable for both
PDF embedding and slide insertion.

Run:
    python build_whitepaper_diagrams.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

# --------------------------------------------------------------------- brand --
NAVY = "#0e1a3a"
TEAL = "#14b8a6"
SLATE = "#2b3548"
PAPER = "#ffffff"
SOFT = "#f4f6fa"
BORDER = "#d7dce5"
INK = "#0e1116"
MUTED = "#5b6473"
ALERT = "#c25b35"
GOOD = "#2d7a4f"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.edgecolor": BORDER,
        "axes.linewidth": 0.6,
    }
)

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "diagrams"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CANVAS_W = 160.0
CANVAS_H = 90.0  # 16:9 area in plot-units; we'll set xlim/ylim accordingly.


def _new_fig():
    fig, ax = plt.subplots(figsize=(16, 9), dpi=150)
    ax.set_xlim(0, CANVAS_W)
    ax.set_ylim(0, CANVAS_H)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor(PAPER)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    return fig, ax


def _box(ax, x, y, w, h, *, fill=PAPER, edge=BORDER, lw=1.0, radius=0.8):
    box = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad=0.02,rounding_size={radius}",
        linewidth=lw,
        edgecolor=edge,
        facecolor=fill,
    )
    ax.add_patch(box)


def _text(ax, x, y, txt, *, color=INK, size=11, weight="normal", ha="center", va="center"):
    ax.text(x, y, txt, color=color, fontsize=size, fontweight=weight, ha=ha, va=va)


def _arrow(ax, x1, y1, x2, y2, *, color=TEAL, lw=1.6, style="-|>"):
    arr = FancyArrowPatch(
        (x1, y1),
        (x2, y2),
        arrowstyle=style,
        mutation_scale=14,
        linewidth=lw,
        color=color,
    )
    ax.add_patch(arr)


def _title_block(ax, title, sub):
    """Draws the page title in the top 10 units of the canvas (y=80..90)."""
    _box(ax, 0, 80, CANVAS_W, 10, fill=NAVY, edge=NAVY, radius=0)
    _text(ax, 4, 86, title, color="white", size=22, weight="bold", ha="left", va="center")
    _text(ax, 4, 82, sub, color="#cdd5e0", size=12, ha="left", va="center")


def _footer(ax, label="TrustNode Edge — Security & Architecture"):
    _text(ax, CANVAS_W - 2, 1.5, label, color=MUTED, size=8, ha="right", va="bottom")


# ============================================================ diagram 1: Purdue
def render_purdue():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "TrustNode in the Purdue Reference Model",
        "Vendor-neutral picture of where TrustNode sits inside an industrial network.",
    )

    # Available content area is roughly y = 4..76
    levels = [
        ("Level 5 — Enterprise", "ERP, BI, corporate IT", 68),
        ("Level 4 — Site business systems", "Plant manager dashboards, scheduling", 58),
        ("Level 3.5 — Industrial DMZ", "Reverse proxy, brokers, security gateways", 48),
        ("Level 3 — Operations / Site", "Historian, MES, asset management", 35),
        ("Level 2 — Supervisory", "SCADA, HMIs", 25),
        ("Level 1 — Control", "PLCs, RTUs, controllers", 15),
        ("Level 0 — Field / Sensors / Actuators", "Valves, motors, transmitters", 5),
    ]
    for label, sub, y in levels:
        _box(ax, 6, y, 80, 8, fill=SOFT, edge=BORDER)
        _text(ax, 8, y + 5.2, label, color=NAVY, size=12, weight="bold", ha="left")
        _text(ax, 8, y + 2.4, sub, color=MUTED, size=10, ha="left")

    # OT/IT boundary line between L3.5 and L3
    ax.plot([4, 156], [46, 46], color=NAVY, lw=1.2, linestyle="--")
    _text(ax, 86, 47.2, "OT / IT boundary", color=NAVY, size=10, weight="bold", ha="right")

    # Cloud annotation pointing at L3.5 / L4
    _box(ax, 100, 56, 52, 10, fill=NAVY, edge=NAVY)
    _text(ax, 126, 63, "TrustNode Cloud Backend", color="white", weight="bold", size=12)
    _text(ax, 126, 59, "FastAPI · JWT · Audit · Tenant scope", color="#cdd5e0", size=10)
    _arrow(ax, 100, 61, 86, 52, color=TEAL, lw=2.0)

    # Edge annotation pointing at L3
    _box(ax, 100, 34, 52, 10, fill=NAVY, edge=NAVY)
    _text(ax, 126, 41, "TrustNode Edge Service", color="white", weight="bold", size=12)
    _text(ax, 126, 37, "Polling worker + local SQLite store", color="#cdd5e0", size=10)
    _arrow(ax, 100, 39, 86, 39, color=TEAL, lw=2.0)

    # Read-only PLC polling pointing at L1
    _box(ax, 100, 14, 52, 10, fill="#fbeed5", edge=ALERT)
    _text(ax, 126, 21, "Read-only PLC polling", color=ALERT, weight="bold", size=12)
    _text(ax, 126, 17, "OPC UA · S7 · EtherNet/IP · Modbus", color=ALERT, size=10)
    _arrow(ax, 100, 19, 86, 19, color=ALERT, lw=2.0)

    _footer(ax)
    out = OUT_DIR / "architecture_purdue.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ================================================ diagram 2: single-customer
def render_single_customer():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Single-customer deployment",
        "Plant floor → edge → cloud → browsers. Only outbound HTTPS crosses the boundary.",
    )

    # Plant floor zone (left)
    _box(ax, 4, 16, 62, 60, fill=SOFT, edge=BORDER)
    _text(ax, 35, 72, "Plant floor (OT VLAN)", color=NAVY, weight="bold", size=13)

    # PLCs row (well-spaced)
    plc_labels = ["Siemens S7", "AB CompactLogix", "OPC UA server", "Modbus meter"]
    for i, label in enumerate(plc_labels):
        x0 = 7 + i * 15
        _box(ax, x0, 56, 13, 9, fill="white", edge=BORDER)
        _text(ax, x0 + 6.5, 60.5, label, size=9, color=INK)

    # Edge box
    _box(ax, 16, 36, 38, 12, fill=NAVY, edge=NAVY)
    _text(ax, 35, 43, "TrustNode Edge", color="white", weight="bold", size=13)
    _text(ax, 35, 39, "Polling worker · SQLite · REST on 127.0.0.1", color="#cdd5e0", size=10)

    # PLC -> Edge arrows
    for i in range(4):
        x0 = 7 + i * 15 + 6.5
        _arrow(ax, x0, 56, x0, 48, color=ALERT, lw=1.4)
    _text(ax, 60, 54, "read-only", color=ALERT, size=10, weight="bold", ha="left")

    # Customer firewall vertical dashed line
    ax.plot([72, 72], [10, 76], color=NAVY, lw=1.0, linestyle="--")
    _text(ax, 72, 78, "Customer firewall (NAT, no inbound rule)",
          color=NAVY, size=10, weight="bold", ha="center")

    # Cloud zone (right)
    _box(ax, 78, 16, 78, 60, fill=SOFT, edge=BORDER)
    _text(ax, 117, 72, "TrustNode Cloud VPS", color=NAVY, weight="bold", size=13)

    _box(ax, 82, 60, 70, 9, fill=NAVY, edge=NAVY)
    _text(ax, 117, 65.5, "nginx — TLS termination", color="white", weight="bold", size=12)
    _text(ax, 117, 62.5, "Single entry point on port 443/TCP", color="#cdd5e0", size=10)

    _box(ax, 82, 48, 70, 9, fill=NAVY, edge=NAVY)
    _text(ax, 117, 53.5, "FastAPI Cloud Backend", color="white", weight="bold", size=12)
    _text(ax, 117, 50.5, "JWT auth · Tenant scope · Audit log", color="#cdd5e0", size=10)

    _box(ax, 82, 36, 70, 9, fill=SLATE, edge=SLATE)
    _text(ax, 117, 41.5, "PostgreSQL / Supabase", color="white", weight="bold", size=12)
    _text(ax, 117, 38.5, "Row-level scope by tenant_id · Provider encrypts at rest",
          color="#cdd5e0", size=10)

    # nginx -> backend -> db
    _arrow(ax, 117, 60, 117, 57, color=TEAL, lw=1.4)
    _arrow(ax, 117, 48, 117, 45, color=TEAL, lw=1.4)

    # Browser users (bottom right)
    _box(ax, 82, 18, 32, 14, fill="white", edge=BORDER)
    _text(ax, 98, 27.5, "/portal/", color=NAVY, weight="bold", size=12)
    _text(ax, 98, 23.5, "Admin & operator UI", color=MUTED, size=10)
    _arrow(ax, 98, 32, 98, 36, color=TEAL, lw=1.4)

    _box(ax, 120, 18, 32, 14, fill="white", edge=BORDER)
    _text(ax, 136, 27.5, "/client/client_view.html", color=NAVY, weight="bold", size=12)
    _text(ax, 136, 23.5, "Customer single-file portal", color=MUTED, size=10)
    _arrow(ax, 136, 32, 136, 36, color=TEAL, lw=1.4)

    # Edge → Cloud arrow across the firewall
    _arrow(ax, 54, 42, 78, 60, color=TEAL, lw=2.4)
    _text(ax, 66, 50, "Outbound HTTPS only", color=TEAL, weight="bold", size=11)
    _text(ax, 66, 47, "No inbound to edge", color=MUTED, size=10)

    _footer(ax)
    out = OUT_DIR / "architecture_single_customer.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ====================================================== diagram 3: multi-tenant
def render_multi_tenant():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Multi-tenant isolation",
        "Many customers share one VPS; each customer sees only their own tenant.",
    )

    # Top: three customer subdomains
    subs = [
        ("customer-a.lsapps.app", "#e8f4ee"),
        ("customer-b.lsapps.app", "#eaf1f9"),
        ("customer-c.lsapps.app", "#f5eaf0"),
    ]
    for i, (label, color) in enumerate(subs):
        x0 = 10 + i * 48
        _box(ax, x0, 65, 42, 9, fill=color, edge=BORDER)
        _text(ax, x0 + 21, 69.5, label, weight="bold", color=NAVY, size=12)

    # Single nginx box
    _box(ax, 40, 51, 80, 9, fill=NAVY, edge=NAVY)
    _text(ax, 80, 55.5, "nginx — TLS + host-based routing", color="white", weight="bold", size=13)

    # Arrows from subdomains to nginx
    for i in range(3):
        x_top = 10 + i * 48 + 21
        _arrow(ax, x_top, 65, 80, 60, color=TEAL, lw=1.3)

    # FastAPI backend
    _box(ax, 40, 39, 80, 9, fill=NAVY, edge=NAVY)
    _text(ax, 80, 43.5, "FastAPI cloud backend", color="white", weight="bold", size=13)
    _arrow(ax, 80, 51, 80, 48, color=TEAL, lw=1.4)

    # Tenant resolution band
    _box(ax, 30, 28, 100, 8, fill="#fff7e6", edge="#e6c089")
    _text(ax, 80, 32, "Tenant resolved from host header   →   JWT tenant_id MUST match",
          weight="bold", color=NAVY, size=12)
    _arrow(ax, 80, 39, 80, 36, color=TEAL, lw=1.4)

    # Database tenant rows
    db_rows = [
        ("tenant=A rows", "#e8f4ee", "A"),
        ("tenant=B rows", "#eaf1f9", "B"),
        ("tenant=C rows", "#f5eaf0", "C"),
    ]
    for i, (label, color, tid) in enumerate(db_rows):
        x0 = 10 + i * 48
        _box(ax, x0, 8, 42, 16, fill=color, edge=BORDER)
        _text(ax, x0 + 21, 19, "PostgreSQL / Supabase", color=NAVY, weight="bold", size=12)
        _text(ax, x0 + 21, 15, label, color=NAVY, size=11)
        _text(ax, x0 + 21, 11, f"WHERE tenant_id = '{tid}'", color=MUTED, size=10, ha="center")
        _arrow(ax, 80, 28, x0 + 21, 24, color=TEAL, lw=1.3)

    _footer(ax)
    out = OUT_DIR / "architecture_multi_tenant.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ======================================================= diagram 4: three roles
def render_three_role():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Three roles, enforced server-side",
        "Same login endpoint, three very different views — all gated by the JWT.",
    )

    roles = [
        (
            "Master / Global Admin",
            "TrustNode developer team",
            ["See all tenants & customers", "Manage tenants & licenses",
             "Audit log read access", "Issue activation codes for any tenant"],
            ["Edge runtime config from web"],
            NAVY,
        ),
        (
            "Customer Admin",
            "Customer's IT/operations lead",
            ["See only own tenant", "Create dashboards & reports",
             "Manage their own users", "Issue activation codes for own tenant"],
            ["Touch other tenants", "Change licenses or modules"],
            TEAL,
        ),
        (
            "Customer Client Viewer",
            "Customer staff / contractor",
            ["Read-only", "Only modules admin enabled",
             "Only own tenant's data", "No admin tools"],
            ["Any write action", "See other users in tenant"],
            "#7c5ec7",
        ),
    ]

    for i, (role, sub, can, cannot, accent) in enumerate(roles):
        x0 = 6 + i * 51
        _box(ax, x0, 6, 47, 68, fill=PAPER, edge=BORDER, lw=1.2)
        # accent band
        _box(ax, x0, 64, 47, 10, fill=accent, edge=accent)
        _text(ax, x0 + 23.5, 70, role, color="white", weight="bold", size=14)
        _text(ax, x0 + 23.5, 66, sub, color="white", size=11)

        # Can list
        _text(ax, x0 + 3, 60, "Can do:", color=GOOD, weight="bold", size=12, ha="left")
        for j, item in enumerate(can):
            _text(ax, x0 + 3, 56 - j * 3, f"✓  {item}", color=INK, size=10, ha="left")

        # Cannot list  -- starts below Can block
        offset = 56 - len(can) * 3 - 3
        _text(ax, x0 + 3, offset, "Cannot do:", color=ALERT, weight="bold", size=12, ha="left")
        for j, item in enumerate(cannot):
            _text(ax, x0 + 3, offset - 3 - j * 3, f"✗  {item}", color=INK, size=10, ha="left")

    _footer(ax)
    out = OUT_DIR / "architecture_three_role.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ===================================================== diagram 5: store-forward
def render_store_forward():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Store-and-forward resilience",
        "What happens during a cloud outage — and what happens when it recovers.",
    )

    # Three state panels (well spaced inside 4..156 wide)
    states = [
        ("1. Normal operation",
         "Edge polls PLCs and streams to cloud in near-real time.", 6, GOOD),
        ("2. Cloud outage",
         "Edge keeps polling. Data accumulates in local SQLite.", 56, ALERT),
        ("3. Reconnect",
         "Edge back-fills cloud incrementally. Plant never noticed.", 106, NAVY),
    ]
    for title, sub, x0, accent in states:
        _box(ax, x0, 40, 48, 32, fill=SOFT, edge=BORDER, lw=1.2)
        _box(ax, x0, 65, 48, 7, fill=accent, edge=accent)
        _text(ax, x0 + 24, 68.5, title, weight="bold", color="white", size=12)
        _text(ax, x0 + 24, 61, sub, color=INK, size=10)

        # Mini PLC + Edge + Cloud row
        # PLC
        _box(ax, x0 + 3, 46, 11, 8, fill="white", edge=BORDER)
        _text(ax, x0 + 8.5, 50, "PLC", color=INK, size=10)
        # Edge
        _box(ax, x0 + 18, 46, 11, 8, fill=NAVY, edge=NAVY)
        _text(ax, x0 + 23.5, 50, "Edge", color="white", size=10, weight="bold")
        # Cloud (dim if outage)
        cloud_fill = NAVY if title != "2. Cloud outage" else "#9aa1ad"
        _box(ax, x0 + 33, 46, 11, 8, fill=cloud_fill, edge=cloud_fill)
        _text(ax, x0 + 38.5, 50, "Cloud", color="white", size=10, weight="bold")

        # arrows
        _arrow(ax, x0 + 14, 50, x0 + 18, 50, color=ALERT, lw=1.6)
        if title == "1. Normal operation":
            _arrow(ax, x0 + 29, 50, x0 + 33, 50, color=TEAL, lw=1.6)
        elif title == "2. Cloud outage":
            ax.plot([x0 + 29, x0 + 33], [50, 50], color="#9aa1ad", lw=1.4, linestyle=":")
            _text(ax, x0 + 24, 43, "(buffering locally)", color=ALERT, size=10, weight="bold")
        else:
            _arrow(ax, x0 + 29, 51, x0 + 33, 51, color=TEAL, lw=1.8)
            _arrow(ax, x0 + 29, 49, x0 + 33, 49, color=TEAL, lw=1.8)
            _text(ax, x0 + 24, 43, "(catching up)", color=GOOD, size=10, weight="bold")

    # Time arrow under panels
    _arrow(ax, 6, 36, 154, 36, color=NAVY, lw=1.0)
    _text(ax, 80, 33, "Time", color=NAVY, size=10)

    # Bottom: data integrity highlight
    _box(ax, 6, 6, 148, 24, fill="#ecfdf5", edge=GOOD, lw=1.2)
    _text(ax, 80, 27, "What the customer never loses", weight="bold", color=GOOD, size=14)
    bullets = [
        "✓ Every PLC reading is timestamped at the edge and stored locally before any cloud sync attempt.",
        "✓ Cloud sync is incremental — never re-uploads what is already there, never misses what wasn't uploaded yet.",
        "✓ A 5-minute or 5-hour cloud outage produces the same plant-floor record: zero gaps.",
        "✓ The PLCs themselves are not aware of the cloud, so production is never affected by cloud-side events.",
    ]
    for i, line in enumerate(bullets):
        _text(ax, 8, 23 - i * 3.5, line, color=INK, size=11, ha="left")

    _footer(ax)
    out = OUT_DIR / "architecture_store_forward.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ====================================================== diagram 6: Plant PC =
def render_topology_plant_pc():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Deployment A — Plant PC / desktop install",
        "Simplest setup: TrustNode runs on a Windows PC inside the plant. No cloud required.",
    )

    # Plant network frame
    _box(ax, 4, 16, 152, 60, fill=SOFT, edge=BORDER)
    _text(ax, 80, 72, "Customer plant network (single VLAN)",
          color=NAVY, weight="bold", size=13)

    # PLC row
    plc_labels = ["Siemens S7", "AB CompactLogix", "OPC UA srv", "Modbus meter"]
    for i, label in enumerate(plc_labels):
        x0 = 10 + i * 22
        _box(ax, x0, 56, 18, 10, fill="white", edge=BORDER)
        _text(ax, x0 + 9, 61, label, size=10, color=INK)

    # Edge box: a desktop PC
    _box(ax, 30, 32, 50, 14, fill=NAVY, edge=NAVY)
    _text(ax, 55, 41, "TrustNode Edge on a desktop PC", color="white",
          weight="bold", size=13)
    _text(ax, 55, 37, "Windows 10/11 · runs as a Service", color="#cdd5e0", size=10)

    # Local SQLite "drum"
    _box(ax, 95, 32, 50, 14, fill=SLATE, edge=SLATE)
    _text(ax, 120, 41, "Local SQLite store", color="white", weight="bold", size=13)
    _text(ax, 120, 37, "Historian · Live · Config · Reports", color="#cdd5e0", size=10)
    _arrow(ax, 80, 39, 95, 39, color=TEAL, lw=2.0)

    # PLC -> Edge
    for i in range(4):
        x0 = 10 + i * 22 + 9
        _arrow(ax, x0, 56, 55, 46, color=ALERT, lw=1.4)
    _text(ax, 78, 50, "read-only", color=ALERT, weight="bold", size=10, ha="left")

    # Operator browser
    _box(ax, 30, 18, 50, 10, fill="white", edge=BORDER)
    _text(ax, 55, 23, "Operator browser (LAN)", color=NAVY, weight="bold", size=12)
    _text(ax, 55, 20, "Hits http://plant-pc:8000 internally", color=MUTED, size=10)
    _arrow(ax, 55, 28, 55, 32, color=TEAL, lw=1.4)

    # "No cloud needed" callout
    _box(ax, 95, 18, 50, 10, fill="#ecfdf5", edge=GOOD)
    _text(ax, 120, 23, "No cloud connection required", color=GOOD,
          weight="bold", size=12)
    _text(ax, 120, 20, "Air-gap friendly · Customer keeps all data", color=GOOD, size=10)

    _footer(ax)
    out = OUT_DIR / "deployment_plant_pc.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ====================================================== diagram 7: IPC panel
def render_topology_ipc_panel():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Deployment B — Industrial PC (IPC) in the electrical panel",
        "Rugged DIN-rail PC mounted inside the cabinet, next to the PLCs.",
    )

    # Cabinet frame
    _box(ax, 4, 8, 70, 70, fill=SOFT, edge=NAVY, lw=2.0)
    _text(ax, 39, 75, "Electrical cabinet (DIN rail)", color=NAVY,
          weight="bold", size=13)

    # DIN rail PLC stack
    for i, label in enumerate(["PLC CPU", "I/O 16", "I/O 32", "Comm Card"]):
        _box(ax, 8 + i * 16, 58, 14, 12, fill="white", edge=BORDER)
        _text(ax, 15 + i * 16, 64, label, color=INK, size=10)

    # IPC mounted on the rail
    _box(ax, 8, 38, 62, 14, fill=NAVY, edge=NAVY)
    _text(ax, 39, 47, "TrustNode IPC", color="white", weight="bold", size=14)
    _text(ax, 39, 43, "Fanless · DIN-rail · 24 VDC · 8–16 GB RAM",
          color="#cdd5e0", size=10)

    # Backplane / panel bus
    for i in range(4):
        x0 = 15 + i * 16
        _arrow(ax, x0, 58, x0, 52, color=ALERT, lw=1.4)
    _text(ax, 39, 55, "Direct PLC network — no internet",
          color=ALERT, weight="bold", size=10)

    # Internal SD/SSD drum
    _box(ax, 8, 20, 30, 12, fill=SLATE, edge=SLATE)
    _text(ax, 23, 28, "Local SSD / SD", color="white", weight="bold", size=11)
    _text(ax, 23, 24, "SQLite + buffer", color="#cdd5e0", size=10)
    _arrow(ax, 23, 38, 23, 32, color=TEAL, lw=1.4)

    # Optional cellular / VPN modem
    _box(ax, 42, 20, 28, 12, fill="white", edge=BORDER)
    _text(ax, 56, 28, "Optional cellular / VPN", color=NAVY, weight="bold", size=11)
    _text(ax, 56, 24, "Outbound HTTPS to cloud", color=MUTED, size=9)
    _arrow(ax, 56, 38, 56, 32, color=TEAL, lw=1.4, style="<|-")

    # Outside-cabinet cloud (optional)
    _box(ax, 86, 32, 64, 32, fill=NAVY, edge=NAVY)
    _text(ax, 118, 56, "Optional cloud portal", color="white",
          weight="bold", size=13)
    _text(ax, 118, 52, "Operator dashboards, reports", color="#cdd5e0", size=10)
    _text(ax, 118, 47, "(Removable — IPC works fully standalone)",
          color=TEAL, size=10, weight="bold")
    _arrow(ax, 74, 26, 86, 48, color=TEAL, lw=1.6, style="<|-")
    _text(ax, 80, 36, "HTTPS\n(if cellular fitted)", color=TEAL, size=9)

    # Browser
    _box(ax, 86, 12, 64, 16, fill="white", edge=BORDER)
    _text(ax, 118, 22, "Operator browser", color=NAVY, weight="bold", size=12)
    _text(ax, 118, 18, "Either via the LAN to the IPC,\n"
                       "or via the cloud portal if internet is fitted.",
          color=MUTED, size=10)
    _arrow(ax, 118, 28, 118, 32, color=TEAL, lw=1.4)

    _footer(ax)
    out = OUT_DIR / "deployment_ipc_panel.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ====================================================== diagram 8: customer server
def render_topology_customer_server():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Deployment C — Customer server in their datacenter",
        "TrustNode on a Linux VM or rack server. Often serves several plants from one box.",
    )

    # Plant 1 box
    for i, (label, x0) in enumerate([("Plant 1", 6), ("Plant 2", 6 + 22), ("Plant N", 6 + 44)]):
        _box(ax, x0, 50, 18, 26, fill=SOFT, edge=BORDER)
        _text(ax, x0 + 9, 73, label, color=NAVY, weight="bold", size=12)
        # Stub PLCs
        _box(ax, x0 + 2, 65, 14, 6, fill="white", edge=BORDER)
        _text(ax, x0 + 9, 68, "PLC", color=INK, size=10)
        _box(ax, x0 + 2, 57, 14, 6, fill="white", edge=BORDER)
        _text(ax, x0 + 9, 60, "OPC UA", color=INK, size=10)
        # Arrow stub down
        _arrow(ax, x0 + 9, 57, x0 + 9, 52, color=ALERT, lw=1.4)

    # Datacenter zone
    _box(ax, 4, 16, 76, 30, fill=SOFT, edge=NAVY, lw=1.6)
    _text(ax, 42, 42, "Customer datacenter VLAN", color=NAVY, weight="bold", size=12)

    # TrustNode server
    _box(ax, 10, 24, 30, 12, fill=NAVY, edge=NAVY)
    _text(ax, 25, 32, "TrustNode server (Linux VM)", color="white",
          weight="bold", size=12)
    _text(ax, 25, 28, "FastAPI · Polling workers per plant", color="#cdd5e0", size=10)

    # On-prem DB
    _box(ax, 44, 24, 32, 12, fill=SLATE, edge=SLATE)
    _text(ax, 60, 32, "Customer-owned PostgreSQL", color="white",
          weight="bold", size=12)
    _text(ax, 60, 28, "Same datacenter · IT backups", color="#cdd5e0", size=10)
    _arrow(ax, 40, 30, 44, 30, color=TEAL, lw=1.6)

    # Arrows down from plants to server (read-only)
    for i in range(3):
        x0 = 6 + i * 22 + 9
        _arrow(ax, x0, 50, 25, 36, color=ALERT, lw=1.4)
    _text(ax, 38, 47, "Read-only polling (internal LAN)",
          color=ALERT, weight="bold", size=10, ha="left")

    # Cloud portal optional (right)
    _box(ax, 90, 38, 60, 24, fill=NAVY, edge=NAVY)
    _text(ax, 120, 56, "Optional cloud portal", color="white",
          weight="bold", size=13)
    _text(ax, 120, 51, "Operator + management dashboards\n"
                       "Outbound HTTPS only", color="#cdd5e0", size=11)
    _arrow(ax, 80, 30, 90, 50, color=TEAL, lw=1.8, style="<|-")
    _text(ax, 84, 40, "HTTPS\n(outbound)", color=TEAL, size=9)

    # Customer office browsers
    _box(ax, 90, 16, 60, 18, fill="white", edge=BORDER)
    _text(ax, 120, 28, "Customer office / shop floor browsers",
          color=NAVY, weight="bold", size=12)
    _text(ax, 120, 23, "Reachable via LAN to the on-prem server,\n"
                       "or via the cloud portal if internet is available.",
          color=MUTED, size=10)
    _arrow(ax, 120, 34, 120, 38, color=TEAL, lw=1.4)

    _footer(ax)
    out = OUT_DIR / "deployment_customer_server.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ====================================================== diagram 9: cloud-bridged
def render_topology_cloud_bridged():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Deployment D — Cloud-bridged (default)",
        "Edge in the plant + portal in the cloud. The reference topology.",
    )

    # Plant area
    _box(ax, 4, 18, 62, 58, fill=SOFT, edge=BORDER)
    _text(ax, 35, 72, "Customer plant (OT VLAN)", color=NAVY, weight="bold", size=12)

    plc_labels = ["S7", "AB", "OPC UA", "Modbus"]
    for i, label in enumerate(plc_labels):
        x0 = 8 + i * 14
        _box(ax, x0, 58, 12, 8, fill="white", edge=BORDER)
        _text(ax, x0 + 6, 62, label, size=10, color=INK)
        _arrow(ax, x0 + 6, 58, 35, 50, color=ALERT, lw=1.2)

    _box(ax, 16, 38, 38, 12, fill=NAVY, edge=NAVY)
    _text(ax, 35, 47, "TrustNode Edge", color="white", weight="bold", size=12)
    _text(ax, 35, 43, "PC, IPC, or VM", color="#cdd5e0", size=10)

    _box(ax, 16, 24, 38, 10, fill=SLATE, edge=SLATE)
    _text(ax, 35, 29, "Local SQLite buffer (store & forward)",
          color="white", weight="bold", size=11)
    _arrow(ax, 35, 38, 35, 34, color=TEAL, lw=1.4)

    # Firewall line
    ax.plot([72, 72], [10, 78], color=NAVY, lw=1.0, linestyle="--")
    _text(ax, 72, 80, "Customer firewall (NAT, outbound only)",
          color=NAVY, size=10, weight="bold", ha="center")

    # Cloud area
    _box(ax, 78, 18, 78, 58, fill=SOFT, edge=BORDER)
    _text(ax, 117, 72, "TrustNode Cloud (you or us)", color=NAVY,
          weight="bold", size=12)

    _box(ax, 82, 60, 70, 8, fill=NAVY, edge=NAVY)
    _text(ax, 117, 64, "nginx — TLS · Single port 443", color="white",
          weight="bold", size=12)

    _box(ax, 82, 48, 70, 8, fill=NAVY, edge=NAVY)
    _text(ax, 117, 52, "FastAPI backend — JWT · Tenant scope · Audit",
          color="white", weight="bold", size=12)

    _box(ax, 82, 36, 70, 8, fill=SLATE, edge=SLATE)
    _text(ax, 117, 40, "PostgreSQL / Supabase — tenant_id RLS",
          color="white", weight="bold", size=12)

    _arrow(ax, 117, 60, 117, 56, color=TEAL, lw=1.4)
    _arrow(ax, 117, 48, 117, 44, color=TEAL, lw=1.4)

    _box(ax, 82, 22, 32, 10, fill="white", edge=BORDER)
    _text(ax, 98, 27, "/portal/", color=NAVY, weight="bold", size=11)
    _box(ax, 120, 22, 32, 10, fill="white", edge=BORDER)
    _text(ax, 136, 27, "/client/client_view.html", color=NAVY,
          weight="bold", size=11)
    _arrow(ax, 98, 32, 98, 36, color=TEAL, lw=1.4)
    _arrow(ax, 136, 32, 136, 36, color=TEAL, lw=1.4)

    # Edge → Cloud
    _arrow(ax, 54, 44, 78, 60, color=TEAL, lw=2.4)
    _text(ax, 66, 54, "Outbound HTTPS\n(store & forward)", color=TEAL,
          weight="bold", size=10)

    _footer(ax)
    out = OUT_DIR / "deployment_cloud_bridged.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ====================================================== diagram 10: multi-plant central
def render_topology_multi_plant():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Deployment E — Multi-plant central historian",
        "One customer, several plants, a central database that aggregates them all.",
    )

    # Three plants top row
    for i, name in enumerate(["Plant A", "Plant B", "Plant C"]):
        x0 = 6 + i * 18
        _box(ax, x0, 56, 14, 20, fill=SOFT, edge=BORDER)
        _text(ax, x0 + 7, 73, name, color=NAVY, weight="bold", size=11)
        _box(ax, x0 + 2, 67, 10, 4, fill="white", edge=BORDER)
        _text(ax, x0 + 7, 69, "PLCs", color=INK, size=9)
        _box(ax, x0 + 2, 60, 10, 5, fill=NAVY, edge=NAVY)
        _text(ax, x0 + 7, 62.5, "Edge", color="white", size=9, weight="bold")
        _arrow(ax, x0 + 7, 67, x0 + 7, 65, color=ALERT, lw=1.2)
        # Outbound HTTPS
        _arrow(ax, x0 + 7, 60, 80, 44, color=TEAL, lw=1.3)

    # Central server / cloud
    _box(ax, 22, 30, 116, 18, fill=NAVY, edge=NAVY)
    _text(ax, 80, 43, "Central TrustNode server (customer datacenter OR cloud)",
          color="white", weight="bold", size=13)
    _text(ax, 80, 38, "FastAPI backend · tenant_id = customer_id · per-plant edge_id",
          color="#cdd5e0", size=10)
    _text(ax, 80, 33, "All plants visible in one portal; per-plant filtering inside",
          color="#cdd5e0", size=10)

    # Customer DB at the bottom
    _box(ax, 30, 14, 50, 12, fill=SLATE, edge=SLATE)
    _text(ax, 55, 22, "PostgreSQL on customer infra", color="white",
          weight="bold", size=11)
    _text(ax, 55, 18, "Schemas, RLS, IT-managed backups", color="#cdd5e0", size=10)

    _box(ax, 90, 14, 50, 12, fill=SLATE, edge=SLATE)
    _text(ax, 115, 22, "Managed cloud DB (alt.)", color="white",
          weight="bold", size=11)
    _text(ax, 115, 18, "Supabase / RDS / Azure Postgres", color="#cdd5e0", size=10)

    _arrow(ax, 55, 30, 55, 26, color=TEAL, lw=1.4)
    _arrow(ax, 115, 30, 115, 26, color=TEAL, lw=1.4)

    # Customer browser
    _box(ax, 6, 14, 18, 12, fill="white", edge=BORDER)
    _text(ax, 15, 22, "Operator", color=NAVY, weight="bold", size=11)
    _text(ax, 15, 18, "browser", color=MUTED, size=10)
    _arrow(ax, 22, 20, 30, 20, color=TEAL, lw=1.2, style="-|>")
    _arrow(ax, 15, 26, 22, 30, color=TEAL, lw=1.2, style="-|>")

    _footer(ax)
    out = OUT_DIR / "deployment_multi_plant.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ===================================================== diagram 11: storage options
def render_storage_options():
    fig, ax = _new_fig()
    _title_block(
        ax,
        "Storage options",
        "Five ways to hold your data — pick what fits IT policy and budget.",
    )

    options = [
        (
            "1. Local SQLite only",
            "Edge-only, single file on disk.",
            ["No cloud, no network DB",
             "Lightest install, smallest attack surface",
             "Backup = copy the SQLite file",
             "Best for: small standalone sites"],
            GOOD,
        ),
        (
            "2. Customer PostgreSQL",
            "Customer hosts the DB themselves.",
            ["Full control by customer IT",
             "Customer's existing BI tools query it",
             "Customer's existing backup policy applies",
             "Best for: mid/large customer with DBAs"],
            TEAL,
        ),
        (
            "3. Managed cloud DB",
            "Supabase, AWS RDS, Azure Postgres.",
            ["Provider handles backups + HA",
             "Encryption-at-rest standard",
             "Pay-as-you-grow scaling",
             "Best for: multi-plant, multi-region"],
            "#3373c4",
        ),
        (
            "4. Hybrid edge + cloud",
            "Edge SQLite + cloud DB mirror.",
            ["Local-only failback",
             "Cloud-level redundancy",
             "Cloud outage = zero data loss",
             "Best for: production-critical sites"],
            "#7c5ec7",
        ),
        (
            "5. Other DBs on request",
            "MySQL, MSSQL, TimescaleDB.",
            ["Supported on request",
             "May require schema adapter",
             "Air-gapped variants possible",
             "Best for: customer-mandated stacks"],
            MUTED,
        ),
    ]

    for i, (title, sub, bullets, accent) in enumerate(options):
        x0 = 4 + i * 31
        _box(ax, x0, 8, 28, 66, fill=PAPER, edge=BORDER, lw=1.2)
        _box(ax, x0, 64, 28, 10, fill=accent, edge=accent)
        _text(ax, x0 + 14, 70, title, color="white", weight="bold", size=13)
        _text(ax, x0 + 14, 66, sub, color="white", size=10)

        for j, b in enumerate(bullets):
            _text(ax, x0 + 2, 60 - j * 5, f"•  {b}", color=INK, size=10, ha="left")

    _footer(ax)
    out = OUT_DIR / "storage_options.png"
    fig.savefig(out, bbox_inches="tight", facecolor=PAPER, pad_inches=0.0)
    plt.close(fig)
    print(out)


# ============================================================= entry point ==
def main():
    render_purdue()
    render_single_customer()
    render_multi_tenant()
    render_three_role()
    render_store_forward()
    render_topology_plant_pc()
    render_topology_ipc_panel()
    render_topology_customer_server()
    render_topology_cloud_bridged()
    render_topology_multi_plant()
    render_storage_options()


if __name__ == "__main__":
    main()
