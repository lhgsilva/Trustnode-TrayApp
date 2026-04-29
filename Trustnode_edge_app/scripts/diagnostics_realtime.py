#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time
from typing import Any

import requests


def _parse_ts(value: str | None) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    if " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T")
    try:
        return dt.datetime.fromisoformat(raw)
    except Exception:
        return None


def _login(base_url: str, username: str, password: str, timeout_s: float) -> str:
    r = requests.post(
        f"{base_url.rstrip('/')}/api/auth/login",
        json={"username": username, "password": password},
        timeout=timeout_s,
    )
    r.raise_for_status()
    token = str((r.json() or {}).get("token") or "").strip()
    if not token:
        raise RuntimeError(f"No token returned by {base_url}/api/auth/login")
    return token


def _delta_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "min": None, "max": None, "avg": None, "p95": None}
    sorted_vals = sorted(values)
    idx_95 = min(len(sorted_vals) - 1, max(0, int(round((len(sorted_vals) - 1) * 0.95))))
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "avg": round(sum(values) / len(values), 3),
        "p95": round(sorted_vals[idx_95], 3),
        "stdev": round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0,
    }


def _sample_live(base_url: str, headers: dict[str, str], samples: int, sleep_s: float, timeout_s: float) -> dict[str, Any]:
    rows = []
    ts_list: list[dt.datetime] = []
    raw_ts: list[str] = []
    for _ in range(samples):
        r = requests.get(
            f"{base_url.rstrip('/')}/api/app-store/live?limit=1",
            headers=headers,
            timeout=timeout_s,
        )
        r.raise_for_status()
        payload = r.json() or {}
        row = (payload.get("rows") or [{}])[0]
        rows.append(row)
        ts = str(row.get("ts") or row.get("ts_utc") or "")
        raw_ts.append(ts)
        parsed = _parse_ts(ts)
        if parsed is not None:
            ts_list.append(parsed)
        time.sleep(sleep_s)

    deltas: list[float] = []
    for a, b in zip(ts_list, ts_list[1:]):
        deltas.append((b - a).total_seconds())
    return {"raw_ts_head": raw_ts[:8], "delta_stats_seconds": _delta_stats(deltas)}


def _fetch_status(base_url: str, headers: dict[str, str], timeout_s: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    r1 = requests.get(f"{base_url.rstrip('/')}/api/plc/gateways/status", headers=headers, timeout=timeout_s)
    r1.raise_for_status()
    out["gateways"] = r1.json() or []
    r2 = requests.get(f"{base_url.rstrip('/')}/api/app-store/inspector", headers=headers, timeout=timeout_s)
    r2.raise_for_status()
    ins = (r2.json() or {}).get("inspector") or {}
    out["sync_outbox_status"] = ins.get("sync_outbox_status") or {}
    out["data_sync"] = ins.get("data_sync") or {}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="TrustNode realtime diagnostics (edge/cloud).")
    ap.add_argument("--local-url", default="http://127.0.0.1:8000")
    ap.add_argument("--cloud-url", default="")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--sleep-s", type=float, default=1.0)
    ap.add_argument("--timeout-s", type=float, default=10.0)
    args = ap.parse_args()

    report: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "local_url": args.local_url,
            "cloud_url": args.cloud_url,
            "samples": args.samples,
            "sleep_s": args.sleep_s,
            "timeout_s": args.timeout_s,
        },
    }

    local_token = _login(args.local_url, args.username, args.password, args.timeout_s)
    local_headers = {"Authorization": f"Bearer {local_token}"}
    report["local"] = {
        "live": _sample_live(args.local_url, local_headers, args.samples, args.sleep_s, args.timeout_s),
        "status": _fetch_status(args.local_url, local_headers, args.timeout_s),
    }

    if args.cloud_url.strip():
        cloud_token = _login(args.cloud_url, args.username, args.password, args.timeout_s)
        cloud_headers = {"Authorization": f"Bearer {cloud_token}"}
        report["cloud"] = {
            "live": _sample_live(args.cloud_url, cloud_headers, args.samples, args.sleep_s, args.timeout_s),
            "status": _fetch_status(args.cloud_url, cloud_headers, args.timeout_s),
        }

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

