"""Apply the per-customer tenancy migration with safety rails.

Two modes:
  --dry-run  (default)  open a transaction, run the migration, capture
                        before/after row counts, ROLLBACK.
  --commit              same as above but COMMIT at the end.

Reads connection info from the project .env, never from CLI args, so
credentials don't appear in process listings.

Captures:
  * row counts of every customer-scoped table BEFORE the migration
  * row counts AFTER (in the same transaction)
  * how many rows moved off tenant_id='default' (per table)
  * any RAISE NOTICE the migration emits at the end
  * leftover 'default'-tagged rows that the migration could not match
    to a customer (these stay master-visible)

Designed to be safe to re-run.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
MIGRATION_FILE = ROOT / "db" / "migrations" / "20260518_per_customer_tenant.sql"


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        raise SystemExit(f".env not found at {ENV_FILE}")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# Tables that should END UP with no 'default'-tagged rows that belong to
# a customer. Used both for before/after counting and for the leftover
# audit at the end. We snapshot the "default" counts on these tables and
# expect them to drop to (or near) zero after the rewrite.
def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script on un-quoted, un-dollar-quoted top-level
    semicolons. Handles single-quoted strings and Postgres dollar-quoted
    blocks ($tag$...$tag$). Does NOT handle line comments inside
    strings (it's a small migration runner, not a full parser)."""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_single = False
    dollar_tag: str | None = None  # name of the currently open $tag$ block, or None
    while i < n:
        ch = sql[i]
        # Inside a $tag$...$tag$ block, swallow everything until the closing tag.
        if dollar_tag is not None:
            close = f"${dollar_tag}$"
            if sql.startswith(close, i):
                buf.append(close)
                i += len(close)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                # Handle '' escape (still inside the literal)
                if i + 1 < n and sql[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            # Try to read a $tag$ — tag is empty or [a-zA-Z_][a-zA-Z0-9_]*
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                tag = sql[i + 1:j]
                dollar_tag = tag
                buf.append(sql[i:j + 1])
                i = j + 1
                continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            # line comment — skip to end of line but keep newline so line
            # numbers in errors still make sense
            j = sql.find("\n", i)
            if j == -1:
                buf.append(sql[i:])
                i = n
            else:
                buf.append(sql[i:j + 1])
                i = j + 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


SCOPED_TABLES = (
    ("cp_edges", "customer_id IS NOT NULL"),
    ("cp_licenses", "customer_id IS NOT NULL"),
    ("cp_edge_activation_codes", "customer_id IS NOT NULL"),
    ("dashboard_configurations", "split_part(scope_key,'|',2) NOT IN ('','-')"),
    ("alarms_setup", "split_part(scope_key,'|',2) NOT IN ('','-')"),
    ("triggers_limits", "split_part(scope_key,'|',2) NOT IN ('','-')"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="actually commit. Without this, rolls back.")
    args = parser.parse_args()

    env = _load_env()
    host = env.get("TRUSTNODE_CLOUD_DB_HOST") or ""
    port = env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"
    dbname = env.get("TRUSTNODE_CLOUD_DB_NAME") or "postgres"
    user = env.get("TRUSTNODE_CLOUD_DB_USER") or ""
    password = env.get("TRUSTNODE_CLOUD_DB_PASSWORD") or ""
    sslmode = env.get("TRUSTNODE_CLOUD_DB_SSLMODE") or "require"
    if not host or not user or not password:
        raise SystemExit("missing one of TRUSTNODE_CLOUD_DB_HOST / _USER / _PASSWORD in .env")

    if not MIGRATION_FILE.exists():
        raise SystemExit(f"migration not found: {MIGRATION_FILE}")
    migration_sql = MIGRATION_FILE.read_text(encoding="utf-8")
    # Strip the migration's OUTER BEGIN/COMMIT — we manage the txn here.
    # Important: BEGIN appears INSIDE DO $$ ... $$ blocks as a body
    # marker; an indiscriminate strip would corrupt the script. We only
    # remove the leading top-level BEGIN and the trailing top-level
    # COMMIT, leaving everything else alone.
    lines = migration_sql.splitlines()
    # Find first non-comment, non-blank line
    first_code = next(
        (i for i, ln in enumerate(lines)
         if ln.strip() and not ln.strip().startswith("--")),
        None,
    )
    if first_code is not None and lines[first_code].strip().rstrip(";").upper() == "BEGIN":
        lines[first_code] = ""
    # Find last non-comment, non-blank line
    last_code = next(
        (i for i in range(len(lines) - 1, -1, -1)
         if lines[i].strip() and not lines[i].strip().startswith("--")),
        None,
    )
    if last_code is not None and lines[last_code].strip().rstrip(";").upper() in {"COMMIT", "ROLLBACK"}:
        lines[last_code] = ""
    migration_sql = "\n".join(lines)

    import psycopg2  # type: ignore
    from psycopg2.extras import RealDictCursor

    print(f"[i] connecting to {user}@{host}:{port}/{dbname} sslmode={sslmode}")
    notices: list[str] = []
    conn = psycopg2.connect(
        host=host, port=int(port), dbname=dbname, user=user, password=password,
        sslmode=sslmode, connect_timeout=15,
    )
    # Capture RAISE NOTICE output
    try:
        conn.notices  # initialise
    except Exception:
        pass
    conn.autocommit = False
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ---- BEFORE counts ----
        print("\n[i] BEFORE migration:")
        before: dict[str, dict[str, int]] = {}
        for tbl, pred in SCOPED_TABLES:
            try:
                cur.execute(f"SELECT count(*) AS total, count(*) FILTER (WHERE tenant_id='default' AND {pred}) AS on_default FROM public.{tbl}")
                row = cur.fetchone() or {}
                before[tbl] = {"total": int(row.get("total") or 0), "on_default": int(row.get("on_default") or 0)}
                print(f"    {tbl:32s} total={before[tbl]['total']:>10}  on_default={before[tbl]['on_default']:>10}")
            except Exception as exc:
                before[tbl] = {"total": -1, "on_default": -1}
                print(f"    {tbl:32s} ERROR before-count: {exc}")
        # Plus the big ones
        for tbl in ("historian_readings", "plc_readings", "live_latest", "app_logs", "cp_users"):
            try:
                cur.execute(f"SELECT count(*) AS total, count(*) FILTER (WHERE tenant_id='default') AS on_default FROM public.{tbl}")
                row = cur.fetchone() or {}
                before[tbl] = {"total": int(row.get("total") or 0), "on_default": int(row.get("on_default") or 0)}
                print(f"    {tbl:32s} total={before[tbl]['total']:>10}  on_default={before[tbl]['on_default']:>10}")
            except Exception as exc:
                before[tbl] = {"total": -1, "on_default": -1}
                print(f"    {tbl:32s} ERROR before-count: {exc}")

        # ---- Customer / tenant inventory ----
        try:
            cur.execute("SELECT count(*) AS n FROM public.cp_customers")
            cust_count = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute("SELECT count(*) AS n FROM public.cp_tenants")
            ten_count = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute("SELECT count(*) AS n FROM public.cp_tenants WHERE tenant_id LIKE 'tenant-%%'")
            per_cust_tenants = int((cur.fetchone() or {}).get("n") or 0)
            print(f"\n[i] cp_customers={cust_count}  cp_tenants={ten_count}  per-customer-tenants(before)={per_cust_tenants}")
        except Exception as exc:
            print(f"[!] could not enumerate customers/tenants: {exc}")

        # ---- Run the migration body ----
        # psycopg2 cursor.execute() goes through the extended-query
        # protocol which insists on single-statement parsing AND
        # interprets %. For a multi-statement migration with DO blocks
        # and CTEs we use the simple-query protocol via psycopg2.extras
        # or, simpler, run it through libpq directly using conn.cursor
        # with mogrify-free path. Trick: psycopg2 ALWAYS interprets
        # %-formatting in execute(), but execute(sql, vars=None) with
        # raw SQL containing no % is fine. Here the safest fix is to
        # use psycopg2's `connection.poll`/`PQexec` equivalent — exposed
        # via cursor.execute when the SQL has been escaped.
        #
        # Practical solution: split the migration on un-quoted semicolons
        # and run each statement separately. The migration text doesn't
        # contain semicolons inside string literals other than inside
        # dollar-quoted blocks, which we treat as atomic.
        # psycopg2.cursor.execute treats `%` as a parameter marker ONLY
        # when params are supplied. With vars=None it leaves them alone.
        # We call execute(stmt) with no params, so the literal % in RAISE
        # NOTICE format strings goes through untouched.
        statements = _split_sql_statements(migration_sql)
        print(f"\n[i] running migration ({len(statements)} statements, {len(migration_sql)} chars) ...")
        t0 = time.monotonic()
        for idx, stmt in enumerate(statements, 1):
            stripped = stmt.strip()
            if not stripped:
                continue
            # Skip comment-only blocks (every line starts with -- or is blank).
            has_code = any(
                ln.strip() and not ln.strip().startswith("--")
                for ln in stripped.splitlines()
            )
            if not has_code:
                continue
            try:
                cur.execute(stripped)
                rc = cur.rowcount
                first_code_line = next(
                    (ln.strip() for ln in stripped.splitlines() if ln.strip() and not ln.strip().startswith("--")),
                    "",
                )[:80]
                print(f"    #{idx:>2} rows={rc:>8}  | {first_code_line}")
            except Exception as exc:
                print(f"\n[FATAL] statement #{idx} failed:")
                print("------ stmt ------")
                preview = stripped if len(stripped) < 400 else stripped[:400] + " ...[truncated]"
                print(preview)
                print("------ end -------")
                raise
        elapsed = time.monotonic() - t0
        print(f"[i] migration executed in {elapsed:.2f}s")
        # psycopg2 collects NOTICE on conn.notices
        for n in conn.notices:
            notices.append(n.rstrip())
        if notices:
            print("\n[i] NOTICE output from migration:")
            for n in notices:
                print(f"    {n}")

        # ---- AFTER counts (still inside the txn) ----
        print("\n[i] AFTER migration:")
        after: dict[str, dict[str, int]] = {}
        for tbl, pred in SCOPED_TABLES:
            try:
                cur.execute(f"SELECT count(*) AS total, count(*) FILTER (WHERE tenant_id='default' AND {pred}) AS on_default FROM public.{tbl}")
                row = cur.fetchone() or {}
                after[tbl] = {"total": int(row.get("total") or 0), "on_default": int(row.get("on_default") or 0)}
                delta = before.get(tbl, {}).get("on_default", 0) - after[tbl]["on_default"]
                print(f"    {tbl:32s} total={after[tbl]['total']:>10}  on_default={after[tbl]['on_default']:>10}  (-{delta} migrated off default)")
            except Exception as exc:
                print(f"    {tbl:32s} ERROR after-count: {exc}")
        for tbl in ("historian_readings", "plc_readings", "live_latest", "app_logs", "cp_users"):
            try:
                cur.execute(f"SELECT count(*) AS total, count(*) FILTER (WHERE tenant_id='default') AS on_default FROM public.{tbl}")
                row = cur.fetchone() or {}
                after[tbl] = {"total": int(row.get("total") or 0), "on_default": int(row.get("on_default") or 0)}
                delta = before.get(tbl, {}).get("on_default", 0) - after[tbl]["on_default"]
                print(f"    {tbl:32s} total={after[tbl]['total']:>10}  on_default={after[tbl]['on_default']:>10}  (-{delta} migrated off default)")
            except Exception as exc:
                print(f"    {tbl:32s} ERROR after-count: {exc}")

        # ---- per-customer-tenant inventory ----
        try:
            cur.execute("SELECT count(*) AS n FROM public.cp_tenants WHERE tenant_id LIKE 'tenant-%%'")
            per_cust_tenants_after = int((cur.fetchone() or {}).get("n") or 0)
            print(f"\n[i] per-customer-tenants(after)={per_cust_tenants_after}")
            cur.execute("SELECT tenant_id FROM public.cp_tenants WHERE tenant_id LIKE 'tenant-%%' ORDER BY tenant_id")
            for row in cur.fetchall():
                print(f"      - {row['tenant_id']}")
        except Exception as exc:
            print(f"[!] post-count enumerate failed: {exc}")

        if args.commit:
            print("\n[!] --commit set: COMMITTING.")
            conn.commit()
            print("[ok] migration committed.")
        else:
            print("\n[!] dry-run: ROLLING BACK. Re-run with --commit to apply for real.")
            conn.rollback()
            print("[ok] rolled back.")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"\n[FATAL] migration failed: {exc}")
        # Surface any notices we did get
        for n in conn.notices:
            print(f"    NOTICE: {n.rstrip()}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
