# Working rules for TrustNode Edge

These apply to every request in this repository. They exist because each one
comes from a real incident, not from style preference.

## 0. Read this before planning any change — 2026-10-28

This section is first on purpose. It is the safeguard to apply BEFORE
planning a fix, an improvement or a feature, and it comes from a day where
each of these was violated in turn.

**The main edge app is the source of everything and has to be at 100%.**
Collecting the data, storing it, and printing it into reports, dashboards,
screens and charts is the product. A LAN surface, a cloud mirror, a portal
or a new feature is worth nothing if the edge itself is not completely
reliable. When a change could trade edge reliability for anything else, it
does not ship.

**Do not break what is working while fixing something else.** Before
changing a shared path, say plainly what else runs through it. A fix that
repairs one surface and costs the main app three points of delivery is a
regression, not a fix — that exact trade happened on 2026-10-27, when the
LAN listeners were given the collection loop's thread budget and WebSocket
delivery fell from 97.0% to 93.5%.

**Find the real problem. Do not guess.** Measure before theorising, and
when a theory is wrong, say so and discard it. On 2026-10-27 the slow boot
was blamed on the WAL (boot was unchanged with the WAL at zero) and then
on a `journal_size_limit` that its own test disproved; the actual cause was
`PRAGMA quick_check` reading 7.3 GB to decide whether to delete files that
did not exist. The measurement found it in one run after a day of plausible
guesses.

**Prefer the easy solution.** No new abstraction, layer, option or
mechanism unless the simple version has been tried and failed. Check
whether the mechanism already exists before writing one — a second
firewall helper was written on 2026-10-27 before noticing that
`windows_firewall.py` already did the job, and a duplicate would have
drifted from the installer's rule.

**Never let a validation run against an app somebody is using.** The
release gate is a 10-minute full-system load test. Run against the
operator's live install it produces exactly the symptoms it is meant to
detect — "Devices could not be saved", "Timed out after 60s" — and wastes
everyone's time chasing a fault that is the measurement itself. Ask before
starting it, or run it when the machine is free.

## 1. Never point at old, legacy or wrong data

Before reporting what the app "is doing", confirm you are reading the store the
app is actually using. Getting this wrong produces confident, false answers.

- Check `/api/health` -> `workspace` and `historian_read_routing` first.
- Config lives in `config_documents_scoped` (keyed `tenant|customer|edge`).
  `config_documents` is the legacy unscoped table and is usually stale.
- Not every `.db` under `~/.trustnode_edge/data` is live. `trustnode_edge.db`
  can be months out of date while the running meter data is elsewhere.
- The same applies to code: do not cite or revive a legacy path, a duplicate
  component or a dead endpoint. If two versions exist, establish which one
  ships before touching either.

If you cannot establish which source is live, say so rather than guessing.

## 1b. Scope is not optional — never let one scope read another

The whole app is scope-defined. Configuration belongs to a
`tenant | customer | edge` (and, for personal settings, a user). One SQLite
file routinely holds documents for SEVERAL customers and edges — a
re-activated install has both — so "read the row for this domain" is never a
safe instruction on its own.

- Site-level config is saved under `tenant|customer|edge` (missing customer is
  the literal `-`). Personal domains add `|username`.
- Build that key with `shared_edge_scope_key()` (no request needed) or
  `_build_scope_key(request, domain=...)`. Do NOT re-derive it by hand: a
  second copy of the rule treated a missing customer as "no scope" and read
  the wrong document.
- A background thread reads a domain with
  `app_store.get_domain_scoped_fast(domain)`. It reads THIS edge's scope only.
- **When the edge has an identity, never fall back to the unscoped document.**
  `config_documents` is shared across every scope in the file; serving it as
  "this edge's config" leaks another customer's setup into this one. A miss
  goes through `get_bootstrap_scoped`, whose candidate list is deliberate and
  bounded.
- Only an edge with NO identity may read/write unscoped, because that is where
  its saves go too.

A cross-scope read is a data-protection failure, not a display bug. If you
cannot resolve the scope, return "unknown" and say so — never substitute
another scope's data.

## 2. Do not touch code that is validated and working

Change the smallest surface that fixes the problem.

- No drive-by refactors, renames or "while I'm here" cleanups.
- No reformatting of files you are not fixing.
- If the fix genuinely requires editing a validated, working path, stop and say
  so, with the reason, before making the change.

## 3. Keep it simple and direct

Prefer the plain solution. No new abstraction, layer or configuration switch
unless the simpler version has been tried and failed. Simple code is what makes
rule 2 possible.

## 4. Plan first, then confirm

Before editing code, configuration or schema: state the plan and its
implications, and wait for approval.

Read-only work needs no approval — investigating, reading files and databases,
running tests, and answering questions. Do that freely, then bring the plan.

## 5. Standing constraints

- **Do not lose data.** When in doubt, keep the reading.
- **The release gate must PASS on the running new build before any commit:**
  `python scripts/validate_release.py`. Not on source alone, not on an older
  build.
- **Never browse the live install with headless automation.** A headless UI
  check blanked three live dashboard widgets on 2026-08-22. Read the API or the
  database instead.
- **Never write to the live workspace from a build or a test.** Importing
  `app.state` migrates whatever database the environment points at.
- A test that cannot fail proves nothing. When you add a guard for a bug,
  reintroduce the bug and confirm the guard catches it.

## 6. The cloud surfaces (portal, Lite, Supabase) — 2026-10-13

These come from a day spent finding out why the portal and Lite "stopped
working". Every one of them cost hours because the symptom pointed somewhere
other than the cause.

### Authentication and authorisation are different stores

A login needs BOTH and they fail independently:

- **Supabase Auth** holds the password. `master@trustnode.local` was never
  lost — it was confirmed, unbanned, and `updated_at` equalled its last
  sign-in, which proves the password had not changed. "Restoring" it would
  have *changed* the thing we were trying to preserve.
- **`cp_users`** holds the role and tenant. That row was missing, which is
  what broke the portal.

`_is_global_admin()` is `role=admin AND tenant_id=="default"` — so the
developer view needs a `default` row in **`cp_tenants`** too. It did not
exist, and `cp_users` has a foreign key onto it: the developer scope itself
had never been created when the control plane moved to the cloud.

**Never leave `cp_users.password_hash` blank.** `_verify_password` ends in
`hmac.compare_digest(password, raw)`, so an empty hash authenticates an
EMPTY password. Use a pbkdf2 hash of random bytes you discard.

### "Failed to fetch" is a network error, not a wrong password

A wrong password returns a clean 400. `Failed to fetch` means the browser got
no response at all. Check, in this order: the bundle's target URL, then the
browser/network.

The actual cause on 2026-09-30: `build_lite_cloud_bundle.py` turned the LAN
fork into the cloud app by swapping ONE importmap entry and nothing else, so
the real Supabase client ran against the LAN placeholder `local://lan`. The
correct `config.json` sat on the VPS with nothing reading it — and the
deployed page's fetch targets never listed it, which was visible from outside
the whole time.

- A build that converts one app into another must **assert the result**, not
  trust the replace. Three guards now fail that build.
- **Bump `CACHE_VERSION` in `sw.js` whenever `index.html` changes.** The shell
  is served cache-first; a new page under an old version reaches nobody.
- Make errors name the host and the build, or a stale cached shell and a
  blocked network look identical in a screenshot.

### Mirrors: metadata first, file in parallel

`upload_and_mirror` returned early when there was no `file_path`, so a
storage failure lost the **metadata too** — the report did not exist in the
cloud at all. Metadata is authoritative: write it unconditionally, upload the
bytes alongside, attach `storage_path` after. `ON CONFLICT` must COALESCE
`storage_path` so the metadata-first write cannot null an existing one.

- Record the **run window** in `meta`. It was `{}`, so a mirrored report said
  which template ran but not over what period — regenerating would silently
  use the template's default range.
- `file_bytes` is the file SIZE (an integer). The content only ever lives in
  the Storage bucket.
- A backfill that skips "when the cloud already has rows" skips exactly when
  there is a backlog worth fixing. Retry the *missing* work, capped per run.

### Deploying

- The canonical deploy is the **GitHub Action on push to `main`**; it builds
  the frontend and ships backend + UI together. `vps_deploy.py --apply` pulls
  and restarts the BACKEND ONLY.
- **Ship the backend and the bundle as a pair.** `dist_cloud_readonly` is a
  committed artefact and goes stale: pushing it unrebuilt regresses the portal
  to whenever it was last built.
- The deploy's health probe runs ~2 s after restart and will report
  `HEALTH_FAILED` before uvicorn is listening. Re-check before believing it.
- A traceback in the log at restart is usually the OLD process shutting down.
  Compare its PID against `MainPID`.
- Verify a build by **artifact timestamps**, never by exit code.
- Many rapid SSH sessions trip fail2ban. Batch remote commands into one
  connection.

### Reading the live system

- Ask for environment **key names**, not values — `systemctl show -p
  Environment` prints secrets into the transcript.
- `/api/health` on the cloud is the HOST's licence, not the selected
  customer's. Per-customer modules come from `cp_licenses` ->
  `cp_license_modules`, and those carry PRODUCT keys (`reporting`,
  `historian`), not the `view.lan_*` spelling.
- A git pathspec is relative to the current directory. From a subdirectory, a
  repo-root path silently matches nothing — and a check that matches nothing
  passes.

## 7. Activation codes — the chain, and why they fail

`scripts/test_onboarding_activation_login.py` asserts all of this. Run it
before shipping anything that touches onboarding.

### The chain, in order

A code cannot be issued unless every link exists and is joined up:

```
tenant -> customer -> licence (active, linked to that customer)
                   -> edge    (linked to that customer)
                   -> code    (carries tenant + customer + edge + licence
                               + the infrastructure endpoints)
```

`issue_activation_code` refuses with a specific reason, and each one is a
real mistake someone has made:

| Reason | What it means |
|---|---|
| `customer_id_required` / `edge_id_required` / `license_id_required` | the portal sent an incomplete form |
| `customer_not_found` / `edge_not_found` | created in a different tenant |
| `edge_not_linked_to_customer` | the edge belongs to someone else — this is how an edge ends up reporting into the wrong customer |
| `license_not_linked_to_customer` | the licence belongs to someone else |
| `license_not_active` | suspended/expired: a lapsed customer must not activate new edges |

### The rules that keep it working

- **Issue under the CUSTOMER's tenant, never the issuer's.** The endpoint
  forces this (`_customer_tenant_id(payload.customer_id)`) regardless of what
  the portal sent. Issuing under the master's `default` puts the edge in the
  wrong scope, and everything it writes afterwards lands there.
- **The code carries the endpoints.** `metadata.infrastructure` is resolved
  (per-tenant over global) at issue time, so a brand-new computer learns
  where the portal lives from the code itself. Nobody types a URL. If you
  re-host, update Portal → Infrastructure *first*, then issue new codes.
- **Be liberal in what you accept.** A pasted code survives surrounding
  whitespace, inner spaces, tabs, CRLF, line wrapping, and hyphens that a
  mail client has typographed into en/em dashes. A wrong code still fails —
  tolerance must never become acceptance.
- **TTL has a 5-minute floor.** A shorter one expires before the operator
  finishes typing.
- **Re-applying a used code is idempotent** — a re-install must not be
  blocked by its own previous activation. It returns the edge the code was
  *issued for* and ignores the `edge_id` presented, so one code can never
  license a second machine.
- **`revoked` and `expired` are refused.** Expiry is also written back on
  first sight, so a stale code stops looking valid.

### Before blaming the code

Activation failures are almost never the code itself. Check, in order:

1. Which **tenant** did it get issued under? (`cp_edge_activation_codes.tenant_id`)
2. Does the **customer/edge/licence** triple actually exist in that tenant?
3. Is the licence **active**?
4. Does `metadata.infrastructure.cloud_api_url` point at the portal the edge
   can actually reach?
5. Only then: is the code `issued`, `used`, `revoked` or `expired`?

### Passwords, while you are in there

**Never write a blank `cp_users.password_hash`.** `_verify_password` now
rejects an empty stored hash outright, but the reason matters: its fallback
is `hmac.compare_digest(password, raw)`, and `compare_digest("", "")` is
`True` — a blank hash authenticated a blank password. `upsert_user` always
hashes, so the exposure is any row written **directly** (a migration, a
repair script, a hand-made developer row). Use a pbkdf2 hash of random bytes
you discard when the password lives elsewhere, e.g. in Supabase Auth.

### Activating a customer who has NOT installed anything yet

This is the case that breaks, because on a brand-new machine there is:

- no `cloud_url` in `app_settings` — that is written **by** activation;
- nothing in `localStorage` — it has never talked to a portal;
- an empty local control-plane store — it has never seen this code;
- and the operator is on `http://127.0.0.1`, so the request's own origin is
  useless as a fallback.

**The activation code cannot carry the address.** It is
`secrets.token_urlsafe(24)` — opaque. The endpoints live in the code's
database ROW, readable only by asking the portal the machine does not yet
know how to reach.

So the address comes from the **shipped bundle**, and activation tries, in
this order (`frontend/src/api.js`):

```
[ explicitCloud , storedCloud , CONTROL_PLANE_FALLBACK_URL ]
  Portal address  what worked    DEFAULT_CONTROL_PLANE_URL
  field           last time      (or VITE_TRUSTNODE_CONTROL_PLANE_URL)
```

Rules that keep a new customer able to come online:

- **Never ship a bundle with no portal address.** On 2026-08-25 that list
  came out EMPTY, the loop never ran, and the operator saw a bare
  "activation failed" with no clue. `test_fresh_install_activation.py` now
  checks the BUILT bundles, not just the source — source being right is not
  evidence of what shipped.
- **The operator's Portal address field wins over everything.** A stale
  stored portal must never overrule someone who has typed the right one.
- **An empty candidate list must say so, and name the field to fill in.**
- **The backend never invents a host.** On localhost with nothing configured
  `_resolve_cloud_control_plane_base` returns `""` by design — guessing would
  send a customer's activation to someone else's deployment. The local
  edge-link endpoint forwards upstream when its own store has never seen the
  code, which is always true on a fresh machine.
- Self-hosting: set `VITE_TRUSTNODE_CONTROL_PLANE_URL` at build time so the
  installer ships pointing at the right portal.

## 8. Two traps that make working code look broken — 2026-10-17

Both of these cost a day each because the code read correctly. Neither was a
crash, and neither logged anything.

### A name can hide a timezone bug

`<input type="datetime-local">` returns LOCAL wall time with no zone
(`2026-10-17T17:08:40`). The Data Export page wrote that straight into a
field called `from_utc`, which the service compares against the historian's
`ts_utc`. On a UTC+1 machine every query asked about an hour that had not
happened yet, so Preview and Export were both empty — the correct answer to
the wrong question. Measured on the live store: the typed window returned
**0 rows**, the same window shifted −1 h returned **27,234**.

- An input shows the clock on the wall. Convert at the API boundary, once,
  and run every path through that one conversion — Preview and the two export
  routes were three separate calls.
- When a field is named `*_utc`, check what actually reaches it. The name is
  what stops you looking.

### ReportLab does not shrink a Paragraph to its text

"Fit to content" for a report pill was implemented by leaving `colWidths`
unset and trusting the Table to size itself. For a cell holding a
`Paragraph` it does the opposite: measured, a 140 pt string in a
`colWidths=None` table wrapped to **510.2 pt** — the whole text block. So the
pill was page-wide either way and `hAlign` had nothing left to move, which
is exactly how it was reported: "it is only working for left".

- A content fit has to be measured with `stringWidth`, at the size the cell
  actually prints — a header pill prints at `font_size * 1.35`, and measuring
  it at `font_size` makes the heading wrap.
- Cap the measured width at the page width, or a long pill runs off it.

### And: two controls on one key look like a broken control

The pill's position and the layout card's "Text alignment" both wrote
`section.align`, while the pill-card editor offered a position but no width
at all. Nothing could move, and the control that could not work was the one
the operator kept pressing. A pill's own placement now lives on `pill_align`
(`align` is still read, so saved templates do not shift) and `pill_fit` is
offered everywhere a pill is.

### A protected collection refuses a legitimate clear, silently

`_PROTECTED_COLLECTIONS` in `routers/app_store.py` refuses to replace a saved
collection with an empty list. It exists because a headless check once blanked
three dashboard widgets. But some of those collections are things an operator
is *allowed* to empty — `alarms_setup` is an event log — and "Clear All
Alarms" only emptied React state, so the save that followed said nothing about
intent and was refused. The frontend's `catch (_) {}` swallowed the refusal:

```
app_logs: "Refused to clear 300 saved item(s) in 'alarms_setup': the request
           carried an empty list."
  12 of them, 2026-08-26 -> 2026-10-17, nobody saw one
```

- A clear must carry the operator's intent to the request: an `*IntentRef` set
  by the button, read by the save, forwarded as `allowEmpty`, and only ever
  for an EMPTY payload. `dashboard_configurations` has done this since
  2026-08-31 — copy that shape rather than inventing a second one.
- **Never swallow a failed config save.** A guard that refuses correctly and a
  save that never happened look identical on screen.
- Checking this is one query: `SELECT ... FROM app_logs WHERE message LIKE
  '%Refused to clear%'` tells you whether a guard is eating someone's work.

### `npm run build -- --flag` goes to the LAST command in the chain

`frontend`'s `build` script is a chain (`vite build && npm run lint:undef &&
npm run smoke`). npm appends forwarded arguments to the end of the whole
string, so `build-web-cloud-readonly.ps1`'s
`npm run build -- --outDir dist_cloud_readonly` handed the flags to
`npm run smoke`. vite built with its DEFAULT output and overwrote
`frontend\dist` — the bundle the desktop app ships — with a cloud-configured
one, while `dist_cloud_readonly` was never written. The script then copied
whatever stale bundle was left from an earlier run and printed "Cloud web
bundle created". Exit code 0 the whole way.

- Invoke the tool directly (`npm exec -- vite build ...`) when you need to pass
  it flags. Never forward through a chained script.
- **A build step must assert its own output exists and is newer than the run.**
  Without that check, a mis-targeted build is indistinguishable from a
  successful one — and the stale copy ships.
- A placeholder default (`https://api.example.com`) plus a WARNING at the end
  of a fifteen-minute build is not a safeguard. Default to the documented
  address.

## 9. `default` is the developer tenant — no customer edge may use it — 2026-11-02

From the owner: "`default` is where we have our development users to
control the licences in the portal. None of customer or new installations
should have a default-tenant user. Every new installation is always linked
to a tenant customer id from the portal — customer, edge, licence,
activation." Different customers may reuse the same usernames (`admin`
above all); the tenant is what keeps them apart, so a shared name must
never block a login and never grant anyone another tenant's rights.

What was actually happening, measured on a live activated edge:

```
historian_readings.tenant_id : default, tenant-cust-11e164d2, ...
cp_users                     : (default, admin), (tenant-cust-11e164d2, admin)
```

- **Loopback meant `default`.** The desktop app always arrives from
  127.0.0.1, and `_resolve_from_host` maps that to `default`. Every request
  that ran before a token existed — the login itself, the Users page's save
  — ran as the developer tenant. Now `tenant.effective_tenant()` resolves a
  request with no tenant of its own to the tenant the edge was ACTIVATED
  into (`app_store._configured_tenant_id()`, cached 15 s in `main.py`).
  The strict token-vs-host 403 keys on the HOST alone: a user whose row
  still says `default` keeps signing in. A lock-out is not a fix.
- **`ControlPlaneStore._seed_defaults` inserted `cp_users (default, admin,
  hash("admin"))` into every store** — every customer edge included — and
  the login's any-tenant fallback accepted it. `admin/admin` opened any
  customer's edge as an admin of the developer tenant. A store never seeds
  a credential; the row is removed at boot while it still has the default
  password, and `_cp_row_usable_here` refuses a `default`-tenant
  control-plane row on an activated edge (the portal, which is not
  activated, keeps using it).
- **The users_access mirror homed new users under `default`** because the
  Users page never sends a tenant. `migrate_from_app_store_payload` takes
  `default_tenant` from the caller; an existing account keeps its tenant.
- **Break-glass sat before the real account.** A real account always wins;
  `admin/admin` answers only on an edge with no usable account of that name.

`test_edge_tenant_is_never_default.py` and
`test_master_never_shadows_customer.py` assert all of it, over HTTP on
loopback.

### And the chart path, same day

Charts read `historian_agg_*`, which only the retired legacy retention path
ever wrote (0 rows on every edge measured). The engine writes
`historian_rollup` — but only inside a pass with an active DELETION policy,
which no customer has. So every wide chart bucketed raw on each refresh:
2.2 s a call for 24 h on a 514k-row store, ~10 s with seven widgets, and
over a tunnel "the edge did not answer in time". Rollups now run with or
without a policy (nothing is deleted) and `get_historian_agg_rows` reads
`historian_rollup`, bucketing raw only for the tail the rollup has not
reached. `test_rollups_feed_charts.py`.

## 10. The report engine — why a 6-page report took ten minutes — 2026-11-02

Measured on a customer-shaped store (2.07M rows, 24 tags at 1 Hz, collection
writing every 500 ms), a 4–6 page template with four 3-series charts, eight
KPIs, a pie and two telemetry tables: **7.0 s → 0.8 s**. At the real customer
(8.5M rows/day, more days, slower disk) the same code took 5–10 minutes.
None of it was "the PDF is big". Each cause scaled with the historian or with
collection load, which is why it never showed on a bench:

- **KPIs and pies aggregated raw rows for the whole window on the WRITER
  connection** (10 s timeout, queued behind collection) — 0.28 s per cell
  here, seconds at the customer. Now `_rollup_window_stats()` takes whole
  minutes from `historian_rollup` (exact: n/sum/min/max) and reads only the
  two edges raw, on `_connect_readonly()`.
- **An empty cell retried UNSCOPED** — no tenant, no usable index, a
  `SCAN historian_readings` per cell, and a cross-scope read. On an edge
  with an identity the retry is gone (`_current_tenant_id() == "default"`
  gates it in the three readers).
- **`_get_app_settings()` was called 200× per render**, each a SELECT under
  the global `app_store._lock` on the writer handle. Cached 5 s, dropped on
  save; `_prefer_cloud_reads` goes through it.
- **Chart tags were matched with `LIKE '%tag%'`** — the tag index unused,
  every bucket of the window scanned per series. Exact first; the substring
  match only when the exact name has no rows.
- **ReportLab drew one `Line` shape per segment** (28 000 shapes, 360 000
  attribute validations). One `PolyLine` per series, and a draw cap of 1 500
  points a reader cannot see.
- **The logo was re-encoded and hashed on every page.** One PDF form per logo.
- Rollups now tick every 60 s (`ROLLUP_TICK_S`), so the raw tail is a minute.

**A generated report exists from the moment its run starts.** Asked for
directly: the row is inserted first (`meta.render_state = "pending"`), the
section DATA is stored with it (`render_data_json`), and it becomes `ready`
(file filled in) or `failed` (reason kept) — never absent. The list shows the
state, previews the data without the PDF, and can regenerate the PDF for the
same window. **The cloud is touched only when `_cloud_sync_possible()`** —
then every state change is mirrored and the data snapshot rides along as a
sidecar. `test_report_data_phase_instant.py`, `test_report_record_first.py`.
