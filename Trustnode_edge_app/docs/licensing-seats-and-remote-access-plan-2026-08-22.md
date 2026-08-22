# Named licence seats, per-user remote access, and the four TrustNode products

Status: **IMPLEMENTED on branch `feature/surfaces-lan-licensing` (edge side, Phases 0-3). Phase 4 is portal work; Phase 5 not started.** See §7.
Date: 2026-08-22 · Author: operator request, drafted against the running 2026-08-22 build
Supersedes nothing; extends `edge-runtime-lan-access-and-view-licensing-plan-2026-08-21.md`
(Phases 0–4 of that plan are implemented and in production use).

---

## 1. What the operator asked for

1. Remote access (LAN and cloud) must be configured **per user**, in the edge's
   own *Users and Access Control* admin — not as a global switch.
2. A customer's licence carries a number of **seats**. The admin **assigns those
   seats to users** created on the edge.
3. The user's **e-mail becomes their login**, and they are **sent the URL** for
   their LAN or Cloud View access.
4. The **portal licence** must describe which of these products and how many
   seats a customer bought.
5. The licensed products are:

| Product | Seats | What it is |
|---|---|---|
| **TrustNode Edge** | max 1 | Runtime service: collection, historian, local database, alarms, reports, batch/OEE/energy engines, cloud sync, licensing |
| **TrustNode Studio** | max 1, part of the basic licence | Admin/configuration interface for engineers and administrators |
| **TrustNode View LAN** | per user licensed | Read-only local browser/client access over the plant LAN |
| **TrustNode Cloud View** | per user licensed | Read-only hosted cloud access against the synchronised cloud database |

6. **Cloud View** is served by the **legacy Lite interface**. **View LAN** is
   per-user selectable: the legacy Lite UI **or** the normal TrustNode app in
   read-only mode with no admin-only menus.

The overriding constraint: *do not break the parts of the app that are
established and working.*

---

## 2. What already exists (2026-08-22, verified on the running build)

This is a smaller job than it looks, because Phases 0–4 of the 2026-08-21 plan
already built the enforcement spine.

| Capability | State |
|---|---|
| Three LAN-served surfaces | `/trustnode/full/app/` (Studio), `/trustnode/client/app/` (React client view), `/trustnode/lite/app/` (legacy Lite) — all served by the edge, all guarded |
| Static surface guard | `access_policy.surface_access()` + `SURFACE_RE`; no session → 302 to `/trustnode/login/?variant=…` (verified 39/39 in the gate) |
| Per-user surface flags | `permissions.access_full` / `access_client` / `access_lite` on the user record |
| Server-side RBAC | `access_policy.evaluate()` — role by method/prefix; viewers can never mutate, verified by the gate |
| Licence module gates | `require_module(key)` → 404; `MODULE_CATALOG` is the live vocabulary |
| Network rule | Remote mutation requires `remote_admin_lan` |
| Sessions | JWT with `tv` claim, revocable, 4 h LAN TTL, lockout, password policy |
| Numeric limits | `max_view_users`, `max_studio_admins`, `max_tags`, `max_gateways_per_edge` |
| Read-only app mode | `isClientViewMode()` + `CLIENT_MODULE_DEFS` page allow-list + `CLIENT_MODULE_PERMISSION_BY_PAGE` |
| Cloud Lite | `web_cloud_readonly/lite` (restored to git 2026-08-21), supabase-js + RLS against the cloud DB |
| E-mail transport | Report scheduler already sends mail (SMTP and a PHP API mode) with saved credentials |
| User records | `cp_users` has `email`; `auth_store.users` does **not** (username is the primary key) |

**The gap is therefore narrow and specific:**

- Licences describe **capabilities**, not **seats per product**.
- `max_view_users` is enforced as a **concurrent-session cap**
  (`view_sessions.py`, 5-minute liveness window), not as **named assignment**.
- Login is by **username**; e-mail is stored but never used to authenticate.
- Nothing sends a user their access URL.
- The LAN read-only surface is *one* fixed UI per install; it is not a per-user
  choice between Lite and the read-only app.

---

## 3. Design

### 3.1 Licence contract (portal → edge)

Extend the signed payload with an explicit `seats` block. Everything else stays
exactly as it is, so an old licence keeps working unchanged:

```json
{
  "license_id": "lic-…",
  "package_key": "operations",
  "seats": {
    "edge_runtime": 1,
    "studio": 1,
    "view_lan": 5,
    "cloud_view": 3
  },
  "modules": [ … unchanged … ],
  "limits":  { … unchanged … }
}
```

**Compatibility rule (important).** `seats` is optional. When absent the edge
derives it and behaves exactly as today:

```
seats.studio      := limits.max_studio_admins   (or unlimited)
seats.view_lan    := limits.max_view_users      (concurrent model retained)
seats.cloud_view  := limits.max_view_users      (concurrent model retained)
seats.edge_runtime:= 1
```

Named-seat enforcement activates **only** when the licence actually carries
`seats`. A customer whose licence has not been reissued sees no change in
behaviour — this is the single most important guard against breaking a working
site.

Product ↔ existing module keys (no new gating vocabulary needed):

| Product | Requires module | Surface |
|---|---|---|
| TrustNode Edge | `gateway_runtime_control` | none (service) |
| TrustNode Studio | `remote_admin_lan` *(for LAN use)* | `/trustnode/full/app/` |
| TrustNode View LAN | `local_web_app` | `/trustnode/client/app/` or `/trustnode/lite/app/` |
| TrustNode Cloud View | `cloud_lite_access` | hosted Lite |

### 3.2 Edge user record

Add three fields. `cp_users` already has `email`; `auth_store.users` needs it.

| Field | Type | Meaning |
|---|---|---|
| `email` | TEXT, unique (case-insensitive) when set | login identity and delivery address |
| `seats` | JSON array | which products this user consumes, e.g. `["studio"]`, `["view_lan","cloud_view"]` |
| `view_ui` | TEXT: `lite` \| `app_readonly` | which UI a View LAN seat is served (default `app_readonly`) |

Existing `access_full` / `access_client` / `access_lite` permissions stay and are
**derived** from `seats` + `view_ui` on save, so every consumer that reads them
today (including the static surface guard) keeps working with no change:

```
studio            → access_full   = true
view_lan+app_readonly → access_client = true
view_lan+lite         → access_lite   = true
cloud_view        → (cloud side; no local surface flag)
```

Migration for existing users: derive `seats` from the current flags
(`access_full → studio`, `access_client|access_lite → view_lan`,
`view_ui` from whichever flag is set). Nobody loses access on upgrade.

### 3.3 Seat accounting

- Seats are **named assignments**, counted from the user table — not from live
  sessions. This is what the operator asked for ("assign the seats bought to
  users").
- Enforced **server-side on user save** (`control_plane.upsert_user`): assigning
  a seat when `assigned >= licensed` returns 409 with a specific message.
- The Users page shows a seat ledger per product: **bought / assigned / free**.
- **Over-assignment after a downgrade** (licence renewed with fewer seats):
  assignments are *kept*, the ledger shows the overage in red, and logins beyond
  the cap are refused oldest-assignment-wins. Never silently unassign a user.
- The concurrent-session cap in `view_sessions.py` stays for legacy licences and
  becomes a no-op when named seats are active.

### 3.4 E-mail as login

- `auth_store` gains `email`, unique when non-empty.
- Login resolves the identity as **username OR e-mail** (case-insensitive),
  in that order. Existing username logins keep working — no forced migration.
- **The JWT `sub` stays the username.** Sessions, the `tv` revocation claim, the
  audit trail and `view_sessions` all key on `sub`; changing it would invalidate
  every session and orphan the audit history. E-mail is an *alias for
  authentication and delivery*, not a new identity key.
- A user created with an e-mail and no username gets one derived from the local
  part (deduplicated), so the internal key always exists.

### 3.5 Sending the access URL

Reuse the report e-mail transport (already configured and working):

- On seat assignment the admin can **Send access e-mail** (also available later
  as *Resend*).
- Content: the surface URLs from `lan_sharing.status` (HTTP and HTTPS, IP and
  hostname forms) for View LAN, or the cloud portal URL for Cloud View; plus a
  first-login link with a one-time password and `must_change_password = 1`.
- Rendered from the existing template machinery; no new mail infrastructure.
- If e-mail is not configured, the dialog shows a **Copy invitation** button so
  the admin can deliver it by hand. Never a dead end.

### 3.6 Surfaces per user

| Seat | `view_ui` | Served |
|---|---|---|
| Studio | — | `/trustnode/full/app/` (needs `remote_admin_lan` from the LAN; admin/engineer role) |
| View LAN | `app_readonly` | `/trustnode/client/app/` — the React app, admin menus hidden, mutations already refused by role |
| View LAN | `lite` | `/trustnode/lite/app/` — the legacy Lite bundle |
| Cloud View | — | hosted Lite against the cloud DB |

`/trustnode/login/` already takes a `variant`; after authentication it should
redirect to the surface the user's seat entitles them to, choosing automatically
when they hold exactly one.

**Read-only app mode** is largely built: `CLIENT_MODULE_DEFS` defines the page
allow-list and per-page permission keys. The work is to serve *that* mode to a
View LAN user on the LAN surface and confirm every admin page is absent from the
menu — with the server-side role check remaining the real enforcement, since
hiding a menu is presentation, never security.

### 3.7 Cloud View authentication (open question — see §6)

The hosted Lite authenticates against Supabase with a tenant realm and RLS. For
named Cloud View seats, each licensed user needs an identity on the cloud side.
Two options, and this needs an owner decision before Phase 4:

- **(a) Mirror users to Supabase Auth** when a Cloud View seat is assigned (edge
  → portal → Supabase). Cleanest for the user (one login everywhere), most
  moving parts.
- **(b) Portal-issued Cloud View accounts**: the edge marks the seat, the portal
  provisions the login and mails it. Less coupling, but the admin manages the
  user in two places.

---

## 4. Rollout — five phases, each independently shippable and gated

Every phase ends with `python scripts/validate_release.py` **and**
`node scripts/ui_smoke.js` passing on the packaged build.

**Phase 0 — contract and shims (no visible change).**
Parse `seats` (with the derivation above), add the three user fields with
migrations that derive values from today's flags, expose a read-only seat ledger
in the API. Nothing enforces anything new. *Risk: near zero — this is additive.*

**Phase 1 — Users and Access Control.**
Seat ledger UI (bought/assigned/free), per-user seat toggles, `view_ui` choice,
e-mail field with validation. Server-side cap on assignment. Derived legacy
flags written on save. *Risk: the user-save path is shared with existing admin
flows — cover it in the surfaces check.*

**Phase 2 — e-mail login + invitations.**
Dual-key login, unique e-mail index, invite/resend mail, copy-invitation
fallback. *Risk: authentication. Mitigate with an explicit test matrix — username
login, e-mail login, wrong case, duplicate e-mail, lockout interaction, master
account, and every existing session must survive.*

**Phase 3 — per-user LAN surface.**
Login redirects by seat; `app_readonly` served over the LAN; menu audit that no
admin page is reachable. *Risk: presentation only; the server-side role gate is
already proven.*

**Phase 4 — portal.**
Licence editor gains per-product seat counts, mirror columns, validation
(`studio ≤ 1`, `edge_runtime ≤ 1` on basic packages). Then Cloud View seat
provisioning per §3.7. *Lives in the portal repo — out of this repo's scope.*

**Phase 5 — retire the duplicated read-only paths** once Cloud View and View LAN
are both driven by seats: freeze vanilla Lite for Cloud View only.

---

## 5. Landmines (things that will break if done carelessly)

1. **Do not change the JWT `sub`.** Everything keys on it — revocation, audit,
   view sessions. E-mail is an alias, not the identity.
2. **Do not switch existing installs to named seats.** Activate only when the
   licence carries `seats`; otherwise keep the concurrent-session model exactly
   as it is today.
3. **Do not remove `access_full` / `access_client` / `access_lite`.** The static
   surface guard reads them. Derive, never delete.
4. **Menu hiding is not access control.** Read-only mode must keep the
   server-side role checks; the gate already proves a viewer gets 403 on
   mutations and 403 on the full bundle.
5. **The desktop loopback path must stay untouched.** The tray and the local
   admin rely on `TRUSTNODE_RBAC_MODE=lan` being log-only on loopback.
6. **Licence downgrade must not lock the admin out.** The Studio seat of the
   account performing the change is never revoked automatically.
7. **`max_view_users` semantics change is customer-visible.** A site that today
   runs 5 casual viewers on 3 concurrent seats will need 5 named seats. This is
   a commercial decision, not just a technical one — flag it before shipping
   Phase 1 to existing customers.

---

## 6. Decisions needed before Phase 1

1. **Does a Studio seat include LAN access**, or must an engineer also hold a
   View LAN seat? *Recommendation: Studio includes it when `remote_admin_lan` is
   licensed — an engineer should not consume a viewer seat.*
2. **Cloud View authentication** — option (a) or (b) in §3.7?
   *Recommendation: (b) first, because it ships without touching Supabase Auth,
   with (a) as a later convenience.*
3. **Default `view_ui` for a new View LAN seat** — `app_readonly` or `lite`?
   *Recommendation: `app_readonly`; Lite is the legacy path.*
4. **Over-assignment policy on downgrade** — refuse logins oldest-wins (proposed)
   or block the licence save until the admin frees seats?
5. **Existing customers**: migrate them to named seats at renewal only
   (proposed), or reissue licences proactively?

---

## 7. Implementation status (2026-08-22)

**Done on the edge, with the owner decisions in §6 taken as recommended** (Studio
includes LAN access; Cloud View accounts provisioned by the portal — option (b);
default `view_ui` = `app_readonly`; over-assignment keeps the assignment and
refuses the extra logins; existing customers migrate at renewal only).

| Piece | Where |
|---|---|
| Seat vocabulary + optional `seats` block, derived for pre-seat licences | `services/license_inspect.py` (`SEAT_PRODUCTS`, `seats_are_explicit()`, `seat_limit()`) |
| One place that knows who holds what, spanning BOTH user stores | `services/seats.py` (`census`, `holders`, `ledger`, `resolve_login_identity`) |
| User record: `email`, `seats_json`, `view_ui`; login by username **or** e-mail | `services/auth_store.py` (`find_by_login`, `email_owner`) |
| Assignment + cap (409 with a readable message) + derived access flags | `routers/control_plane.py` (`_seat_apply`, `_seat_preserve`) |
| Seat ledger API | `GET /api/control-plane/license/seats` |
| Invitation with the right URL per seat, always renderable for copy/paste | `POST /api/control-plane/users/access-email` |
| Remote surface entitlement by seat + `view_ui` | `services/access_policy.py` (`surface_access`) |
| Login gate now delegates to the same decision and reports `entitled` | `routers/lite_local.py` `/check-access` + `static/login/index.html` |
| Seat ledger card, per-user seat controls, invitation dialog | `frontend/src/App.jsx`, `frontend/src/api.js` |

**Proven:** AuthStore seats/e-mail 13/13 · live end-to-end 25/25 (assignment,
derivation for both `view_ui` values, e-mail login with the JWT `sub` unchanged,
invitation targeting the right surface, unmanaged saves preserving seats) · cap
and entitlement on a synthetic seat-bearing licence 13/13 (including "a pre-seat
licence still honours the access flag alone") · surfaces suite 43/43 · UI smoke
7/7 · the seat ledger rendered against the live licence.

**Found and fixed on the way:** `/retention/v2/status` spent 2-3 s per call in
`measured_row_costs()` re-running `COUNT(*)` on 9.4 M rows *after* the part of
the response the page reads — now cached and pre-warmed, **6.5 s → 0.02 s**, with
per-stage timing so a future regression names itself.

**Not done here:** Phase 4 (portal licence editor seat counts, Cloud View
provisioning) lives in the portal repo. Phase 5 (retiring the duplicated
read-only paths) waits until Cloud View is seat-driven.
