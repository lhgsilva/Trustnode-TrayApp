# Working rules for TrustNode Edge

These apply to every request in this repository. They exist because each one
comes from a real incident, not from style preference.

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
