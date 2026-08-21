# TrustNode Edge — Remote Access over the LAN (operator guide)

Date: 2026-08-21 · Applies to builds that include the Remote Access page (Connections → Remote Access).

TrustNode Edge runs on one machine (a desktop PC or an IPC inside the panel). **Remote Access** lets people on the same network open the edge in a browser — on a PC, a tablet or a phone — without installing anything. Three surfaces are served:

| Surface | URL path | Who | What |
|---|---|---|---|
| **TrustNode Edge** (full runtime) | `/trustnode/full/` | admin, engineer | the complete software: configuration, gateways, dashboards, reports, users |
| **TrustNode Local View** | `/trustnode/client/` | viewers (and anyone with the "Local View" access flag) | read-only dashboards and reports, mobile friendly |
| Lite (legacy) | `/trustnode/lite/` | legacy read-only | kept for existing installs; being replaced by Local View |

Everything is served by the edge itself. No cloud, no port forwarding, nothing leaves the site.

## 1. Turn it on

1. Sign in to the edge desktop as an **admin**.
2. Connections → **Remote Access** → **Turn ON**. The first time, a short wizard explains what becomes reachable and recommends HTTPS.
3. The page lists the URLs, one set per network address of the machine, for HTTP (port 8088) and HTTPS (port 8443), plus a hostname form such as `http://LINE3-IPC:8088/trustnode/full/`. Every URL has **Copy** and a **QR code** (scan it with a phone).
4. The tray icon (right-click → Remote Access) shows the same URLs.

The setting survives restarts: the listeners come back with the edge.

## 2. Give people access

Users are managed in **User and Access Control**. For remote use:

- **Role** decides what a person may do: `admin` and `engineer` may configure from the network; `operator` may run operations (acknowledge alarms, start/stop batches, run reports, start/stop gateways); `viewer` is read-only. Viewers can never change configuration, even with a direct API call.
- **LAN Web Access** flags decide which surface a person may open: *TrustNode Edge (full app over LAN)*, *TrustNode Local View (read-only over LAN)*, *Lite (legacy)*. Admins and engineers may always open the full app; viewers need the Local View flag.
- Passwords: admin/engineer accounts need at least 12 characters with letters and digits; others at least 8. Five wrong passwords lock an account for 15 minutes (an admin can unlock it from Remote Access → sessions).
- The built-in master account (`admin`) with its default password **never works from the network** — set `TRUSTNODE_MASTER_ADMIN_PASSWORD` or use named admin accounts.

Remote sessions last 4 hours (12 hours on the desktop). The **Active remote sessions** table shows who is connected from where; **Revoke** signs a user out everywhere immediately.

## 3. HTTP or HTTPS?

Remote Access must work on any company network, so **both** are offered:

- **HTTP (`:8088`)** works immediately on any device. The connection is not encrypted — on a trusted plant network most sites accept this. The page shows a warning banner while HTTP is on.
- **HTTPS (`:8443`)** is recommended. The edge creates its own certificate (valid 10 years, covering the machine's name and addresses). Because it is self-signed, a browser shows a warning until the certificate is trusted:
  - **Download certificate** on the Remote Access page (also at `/api/lan-sharing/certificate`).
  - **Windows**: double-click the `.crt` → Install Certificate → Local Machine → "Trusted Root Certification Authorities".
  - **Android**: Settings → Security → Encryption & credentials → Install a certificate → CA certificate.
  - **iOS**: open the file (Mail/Files) → Settings → Profile Downloaded → Install, then Settings → General → About → Certificate Trust Settings → enable.
  - Sites with their own CA can drop `custom.crt` + `custom.key` into `<data dir>\lan_tls\` and restart Remote Access.
- **HTTPS only** (switch on the page) turns the HTTP listener off once every device trusts the certificate.

## 4. Network and firewall

- Ports: 8088 (HTTP, falls back to 8089–8092 if busy), 8443 (HTTPS, falls back to 8444–8447). The desktop itself keeps using 127.0.0.1:8000, which is never exposed.
- The edge adds a Windows Firewall rule for its own program on the **Private and Domain** profiles only. If the machine's network is classified as *Public*, change the network profile or add an inbound rule for the program on that profile.
- Bind to one interface only (e.g. the office NIC, not the PLC VLAN): Remote Access → advanced → bind address.
- Name resolution: Windows PCs resolve the machine name on the same subnet; phones usually need the IP or `<name>.local`.
- No inbound rule is needed on the plant firewall — all traffic stays inside the LAN.

## 5. What a remote user cannot do (by design)

- Use desktop-only tools (folder pickers, workspace detection) — the page shows "Available on the edge desktop app".
- Configure anything with a `viewer` or `operator` role — the API refuses (403) and the attempt is logged.
- Open the full runtime when the licence has no **TrustNode Edge over LAN** permission (`remote_admin_lan`) — licences issued before 2026-08-21 inherit it from *LAN Sharing & LAN Web Access*.
- Use a no-login share link unless the licence includes **Local View share links**; otherwise Local View always asks for a login.

## 6. Troubleshooting

| Symptom | Check |
|---|---|
| URL does not load from another PC | Remote Access shows *Running*? Same subnet? Firewall profile is Private/Domain? Try the IP instead of the name. |
| Browser warns about the certificate | Expected for self-signed HTTPS — install the certificate (section 3) or use the HTTP URL. |
| "Account temporarily locked" | 5 wrong passwords; wait 15 min or ask an admin to unlock. |
| "Access to this surface is not allowed" | Missing LAN Web Access flag or role too low for the full app. |
| 404 on `/trustnode/full/` from the LAN | Licence lacks `remote_admin_lan` (and has a package key) — renew the licence in the portal. |
| Everything works on the desktop but not remotely | Look at Customer Log → category *access*: every refusal is logged with user, role, path and IP. |

## 7. For support: knobs

Environment variables (set in `%LOCALAPPDATA%\TrustNode\.env`):

- `TRUSTNODE_RBAC_MODE` = `lan` (default: enforce for remote clients, log-only on the desktop) | `enforce` | `log` | `off`
- `TRUSTNODE_LICENSE_GATES` = `lan` (default) | `enforce` | `log` | `off`
- `TRUSTNODE_MASTER_ADMIN_PASSWORD` — required for the master account to be usable remotely

Every refusal (role, licence, surface) is written to `cp_security_audit_log` and to the customer log (category `access`).

## 8. 2026-08-21 fixes in this area

- Switching **HTTPS only** on or off now reuses the same ports. Previously the outgoing listener could keep its socket while a browser held a connection open, so the new listener climbed one port (8443 → 8444) on every toggle and both answered. `stop()` now force-closes the socket it owns and logs when it has to.
- The release gate's surface checks run over HTTPS when a site is HTTPS-only. Before, they keyed off the HTTP port and silently skipped every remote check (reporting a green 16/16 instead of the full 33).
