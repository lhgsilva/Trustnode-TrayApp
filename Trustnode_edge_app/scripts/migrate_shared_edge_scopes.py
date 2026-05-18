"""One-shot migration: collapse per-user shared domains to per-edge scope.

Earlier builds stored every configuration domain under a per-user scope
key like `tenant|customer|edge|username`, so creating a second user on
the same edge gave them an empty gateway list, no databases, no alarm
rules, no reports — the company's assets were sitting in someone else's
scope. The release that introduces shared-edge scopes drops the `|user`
segment for company-shared domains.

This script:

  1. Reads every `config_documents_scoped` row whose domain is in the
     shared set (gateway_configurations, database_configurations,
     power_management_config, devices, triggers_limits, alarms_setup,
     reporting_setup, tags, email_notifications).
  2. Groups them by the prefix `tenant|customer|edge`.
  3. For each group, picks the most-recently-updated row as the
     canonical company copy.
  4. Writes that payload into the new per-edge scope key (no trailing
     user segment).
  5. Deletes the original per-user rows for these shared domains.

Personal domains (`dashboard_configurations`, `app_settings`, etc.) are
left alone — each user keeps their own.

Run once on each edge after upgrading the backend:
    python scripts/migrate_shared_edge_scopes.py

Safe to re-run (idempotent). Prints a summary and never deletes data
without writing the per-edge row first.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


SHARED_EDGE_DOMAINS = {
    "gateway_configurations",
    "database_configurations",
    "power_management_config",
    "devices",
    "triggers_limits",
    "alarms_setup",
    "reporting_setup",
    "tags",
    "email_notifications",
}


def resolve_local_db() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return Path(base) / "trustnode_app_store.db"
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def edge_prefix_of(scope_key: str) -> str | None:
    """Return `tenant|customer|edge` from a scope key, or None if the
    key doesn't have the per-user 4-segment shape we want to migrate."""
    parts = (scope_key or "").split("|")
    if len(parts) != 4:
        return None  # already per-edge, or shape unknown — leave alone
    tenant, customer, edge, _user = parts
    if not edge:
        return None
    return f"{tenant}|{customer or '-'}|{edge}"


def main() -> int:
    db_path = resolve_local_db()
    if not db_path.is_file():
        print(f"local DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row

    # Read all candidate rows
    rows = conn.execute(
        f"""
        SELECT scope_key, domain, payload_json, version, updated_utc
          FROM config_documents_scoped
         WHERE domain IN ({','.join(['?'] * len(SHARED_EDGE_DOMAINS))})
        """,
        tuple(SHARED_EDGE_DOMAINS),
    ).fetchall()
    print(f"-- found {len(rows)} candidate row(s) across {len(SHARED_EDGE_DOMAINS)} shared domain(s) --")

    # Bucket by (edge_prefix, domain) → list of rows
    buckets: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    untouched: list[sqlite3.Row] = []
    for r in rows:
        ep = edge_prefix_of(str(r["scope_key"] or ""))
        if not ep:
            untouched.append(r)
            continue
        buckets[(ep, str(r["domain"] or ""))].append(r)

    if untouched:
        print(f"   ({len(untouched)} row(s) already per-edge or unknown shape — leaving alone)")

    if not buckets:
        print("\nNothing to migrate.")
        conn.close()
        return 0

    # For each (edge, domain), pick the BEST per-user copy and write it
    # under the per-edge scope key, then delete the original per-user rows.
    #
    # "Best" = the row that has the most configured items, breaking ties
    # by most-recently-updated. Previous version of this script picked
    # purely by timestamp, which sometimes promoted a near-empty config
    # over a richer one just because a different user happened to open
    # the app last. Sorting by item-count first preserves the company's
    # real setup.
    def _content_score(row: sqlite3.Row) -> tuple[int, int, str]:
        """Returns (item_count, payload_bytes, updated_utc) for sort."""
        pj = str(row["payload_json"] or "")
        try:
            parsed = json.loads(pj) if pj.strip() else None
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            items = len(parsed)
        elif isinstance(parsed, dict):
            # Many config domains store the real list under a nested key
            # (devices, configurations, …). Sum the lengths of any list
            # values so a {"devices": [...]} bundle outranks an empty {}.
            items = 0
            for v in parsed.values():
                if isinstance(v, list):
                    items += len(v)
            if items == 0 and parsed:
                items = 1
        else:
            items = 0
        return (items, len(pj), str(row["updated_utc"] or ""))

    moved = 0
    deleted = 0
    with conn:
        for (edge_prefix, domain), group in sorted(buckets.items()):
            sorted_group = sorted(group, key=_content_score, reverse=True)
            canonical = sorted_group[0]

            new_skey = edge_prefix
            new_version = int(canonical["version"] or 1)
            payload_json = str(canonical["payload_json"] or "null")
            updated_utc = str(canonical["updated_utc"] or "")

            # Insert/upsert the per-edge row
            existing = conn.execute(
                "SELECT version FROM config_documents_scoped WHERE scope_key=? AND domain=?",
                (new_skey, domain),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE config_documents_scoped
                       SET payload_json = ?, version = ?, updated_utc = ?
                     WHERE scope_key = ? AND domain = ?
                    """,
                    (payload_json, new_version, updated_utc, new_skey, domain),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO config_documents_scoped(scope_key, domain, payload_json, version, updated_utc)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (new_skey, domain, payload_json, new_version, updated_utc),
                )

            # Delete the per-user copies (skip the new per-edge row in case
            # the script gets re-run after a partial earlier pass).
            n_deleted = 0
            for r in group:
                old_skey = str(r["scope_key"] or "")
                if old_skey == new_skey:
                    continue
                cur = conn.execute(
                    "DELETE FROM config_documents_scoped WHERE scope_key=? AND domain=?",
                    (old_skey, domain),
                )
                n_deleted += int(cur.rowcount or 0)

            moved += 1
            deleted += n_deleted
            short_src = ", ".join(sorted({str(r["scope_key"] or "").split("|")[-1] for r in group}))
            print(f"   + {domain:28s}  -> {new_skey:30s}  "
                  f"(from users: {short_src}, dropped {n_deleted})")

    conn.close()
    print(f"\nDone. {moved} (edge,domain) pair(s) collapsed; {deleted} per-user row(s) removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
