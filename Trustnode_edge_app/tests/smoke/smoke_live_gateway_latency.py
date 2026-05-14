#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
import websockets


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts_ms(raw: str) -> float | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    txt = txt.replace(" ", "T")
    if not (txt.endswith("Z") or "+" in txt[10:] or "-" in txt[10:]):
        txt = f"{txt}Z"
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp() * 1000.0
    except Exception:
        return None


def median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(max(0, min(len(s) - 1, round((len(s) - 1) * p))))
    return float(s[idx])


def req(session: requests.Session, method: str, url: str, **kwargs):
    last = None
    for attempt in range(1, 6):
        try:
            return session.request(method, url, timeout=20, **kwargs)
        except requests.RequestException as exc:
            last = exc
            if attempt == 5:
                raise
            time.sleep(0.25 * attempt)
    raise RuntimeError(f"request failed: {last}")


def build_ws_url(base_url: str, token: str) -> str:
    p = urlparse(base_url)
    scheme = "wss" if p.scheme == "https" else "ws"
    host = p.netloc
    return f"{scheme}://{host}/ws/stream?token={token}"


async def run_ws_capture(ws_url: str, duration_s: int, gateway_id: str, tag_name: str):
    ws_lag_ms: list[float] = []
    interarrival_ms: list[float] = []
    last_rx_ms: float | None = None
    sample_count = 0
    by_gateway_tag: dict[str, int] = {}

    end_mono = time.monotonic() + max(5, int(duration_s))

    async with websockets.connect(ws_url, max_size=2_000_000, ping_interval=20, ping_timeout=20) as ws:
        while time.monotonic() < end_mono:
            timeout = max(0.5, end_mono - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            rx_ms = time.time() * 1000.0
            if last_rx_ms is not None:
                interarrival_ms.append(max(0.0, rx_ms - last_rx_ms))
            last_rx_ms = rx_ms

            try:
                data = json.loads(raw)
            except Exception:
                continue
            readings = data.get("readings") if isinstance(data, dict) else None
            if not isinstance(readings, list):
                continue
            gateway = str(data.get("gateway_id") or "")
            for r in readings:
                tag = str((r or {}).get("tag_name") or "")
                if not tag:
                    continue
                key = f"{gateway}::{tag}"
                by_gateway_tag[key] = by_gateway_tag.get(key, 0) + 1
                if gateway_id and gateway != gateway_id:
                    continue
                if tag_name and tag != tag_name:
                    continue
                ts_ms = parse_ts_ms(str((r or {}).get("ts_utc") or ""))
                if ts_ms is None:
                    continue
                ws_lag_ms.append(max(0.0, rx_ms - ts_ms))
                sample_count += 1

    return {
        "sample_count": sample_count,
        "ws_lag_ms": ws_lag_ms,
        "interarrival_ms": interarrival_ms,
        "gateway_tag_counts": by_gateway_tag,
    }


def find_latest_row(rows: list[dict[str, Any]], gateway_id: str, tag_name: str) -> dict[str, Any] | None:
    latest = None
    latest_ms = -1.0
    for row in rows:
        gid = str(row.get("gateway_id") or "")
        tag = str(row.get("tag") or row.get("tag_name") or "")
        if gateway_id and gid != gateway_id:
            continue
        if tag_name and tag != tag_name:
            continue
        ts_ms = parse_ts_ms(str(row.get("ts") or row.get("ts_utc") or ""))
        if ts_ms is None:
            continue
        if ts_ms > latest_ms:
            latest_ms = ts_ms
            latest = row
    return latest


def run_historian_probe(session: requests.Session, base_url: str, duration_s: int, gateway_id: str, tag_name: str):
    seen_ts: set[str] = set()
    lag_ms: list[float] = []
    rows_seen = 0
    loop_end = time.monotonic() + max(5, int(duration_s))

    while time.monotonic() < loop_end:
        try:
            res = req(session, "GET", f"{base_url}/api/app-store/live", params={"limit": 2500})
            if res.status_code >= 400:
                time.sleep(0.5)
                continue
            body = res.json() if res.text else {}
            rows = body.get("rows") if isinstance(body, dict) else []
            if not isinstance(rows, list):
                rows = []
            latest = find_latest_row(rows, gateway_id, tag_name)
            if latest:
                ts_raw = str(latest.get("ts") or latest.get("ts_utc") or "")
                if ts_raw and ts_raw not in seen_ts:
                    seen_ts.add(ts_raw)
                    ts_ms = parse_ts_ms(ts_raw)
                    if ts_ms is not None:
                        lag_ms.append(max(0.0, (time.time() * 1000.0) - ts_ms))
                        rows_seen += 1
        except Exception:
            pass
        time.sleep(0.25)

    return {
        "rows_seen": rows_seen,
        "historian_lag_ms": lag_ms,
    }


def run_status_flicker_probe(session: requests.Session, base_url: str, duration_s: int):
    transitions: dict[str, int] = {}
    last_state: dict[str, bool] = {}
    samples = 0
    end = time.monotonic() + max(5, int(duration_s))

    while time.monotonic() < end:
        try:
            res = req(session, "GET", f"{base_url}/api/plc/gateways/status")
            if res.status_code < 400:
                rows = res.json() if res.text else []
                if isinstance(rows, list):
                    samples += 1
                    for row in rows:
                        gid = str((row or {}).get("gateway_id") or "")
                        if not gid:
                            continue
                        running = bool((row or {}).get("running"))
                        if gid in last_state and last_state[gid] != running:
                            transitions[gid] = transitions.get(gid, 0) + 1
                        last_state[gid] = running
        except Exception:
            pass
        time.sleep(1.0)

    return {
        "status_samples": samples,
        "transitions": transitions,
        "last_state": last_state,
    }


def run_interval_truth_probe(session: requests.Session, base_url: str):
    configured: dict[str, int] = {}
    runtime: dict[str, int] = {}
    mismatches: dict[str, dict[str, int]] = {}
    try:
        boot = req(session, "GET", f"{base_url}/api/app-store/bootstrap")
        if boot.status_code < 400:
            body = boot.json() if boot.text else {}
            rows = body.get("gateway_configs") if isinstance(body, dict) else []
            if isinstance(rows, list):
                for row in rows:
                    gid = str((row or {}).get("id") or "")
                    if not gid:
                        continue
                    configured[gid] = int((row or {}).get("interval_ms") or 0)
    except Exception:
        pass
    try:
        st = req(session, "GET", f"{base_url}/api/plc/gateways/status")
        if st.status_code < 400:
            rows = st.json() if st.text else []
            if isinstance(rows, list):
                for row in rows:
                    gid = str((row or {}).get("gateway_id") or "")
                    if not gid:
                        continue
                    runtime[gid] = int((row or {}).get("interval_ms") or 0)
    except Exception:
        pass
    for gid, cfg_ms in configured.items():
        rt_ms = int(runtime.get(gid, 0))
        if cfg_ms > 0 and rt_ms > 0 and abs(cfg_ms - rt_ms) > max(25, int(cfg_ms * 0.05)):
            mismatches[gid] = {"configured_ms": cfg_ms, "runtime_ms": rt_ms}
    return {
        "configured_interval_ms_by_gateway": configured,
        "runtime_interval_ms_by_gateway": runtime,
        "mismatches": mismatches,
    }


def main():
    ap = argparse.ArgumentParser(description="Smoke diagnostic for live gateway -> historian -> dashboard latency")
    ap.add_argument("--base-url", default=os.environ.get("TN_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--username", default=os.environ.get("TN_ADMIN_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("TN_ADMIN_PASS", "admin"))
    ap.add_argument("--gateway-id", default=os.environ.get("TN_GATEWAY_ID", ""))
    ap.add_argument("--tag", default=os.environ.get("TN_TAG_NAME", ""))
    ap.add_argument("--duration", type=int, default=int(os.environ.get("TN_SMOKE_DURATION_SECONDS", "40")))
    ap.add_argument("--report", default=os.environ.get("TN_SMOKE_REPORT", "tests/reports/live_gateway_latency_report.json"))
    ap.add_argument("--max-ws-p95-ms", type=float, default=float(os.environ.get("TN_MAX_WS_P95_MS", "1500")))
    ap.add_argument("--max-historian-p95-ms", type=float, default=float(os.environ.get("TN_MAX_HIST_P95_MS", "2500")))
    ap.add_argument("--max-flickers", type=int, default=int(os.environ.get("TN_MAX_FLICKERS", "2")))
    args = ap.parse_args()

    base_url = str(args.base_url).rstrip("/")
    session = requests.Session()

    login_res = req(
        session,
        "POST",
        f"{base_url}/api/auth/login",
        json={"username": args.username, "password": args.password},
    )
    if login_res.status_code >= 400:
        raise SystemExit(f"Login failed ({login_res.status_code}): {login_res.text[:300]}")
    token = str((login_res.json() if login_res.text else {}).get("token") or "")
    if not token:
        raise SystemExit("Login succeeded but token is missing")
    session.headers.update({"Authorization": f"Bearer {token}"})

    ws_url = build_ws_url(base_url, token)
    started_utc = iso_now()

    ws_result = asyncio.run(run_ws_capture(ws_url, args.duration, args.gateway_id, args.tag))
    hist_result = run_historian_probe(session, base_url, args.duration, args.gateway_id, args.tag)
    status_result = run_status_flicker_probe(session, base_url, args.duration)
    interval_result = run_interval_truth_probe(session, base_url)

    ws_lag = ws_result["ws_lag_ms"]
    hist_lag = hist_result["historian_lag_ms"]
    interarrival = ws_result["interarrival_ms"]

    report = {
        "started_utc": started_utc,
        "finished_utc": iso_now(),
        "base_url": base_url,
        "gateway_id": args.gateway_id,
        "tag_name": args.tag,
        "duration_seconds": args.duration,
        "ws": {
            "sample_count": ws_result["sample_count"],
            "lag_ms": {
                "avg": round(statistics.fmean(ws_lag), 2) if ws_lag else 0.0,
                "p50": round(median(ws_lag), 2),
                "p95": round(pct(ws_lag, 0.95), 2),
                "max": round(max(ws_lag), 2) if ws_lag else 0.0,
            },
            "interarrival_ms": {
                "avg": round(statistics.fmean(interarrival), 2) if interarrival else 0.0,
                "p50": round(median(interarrival), 2),
                "p95": round(pct(interarrival, 0.95), 2),
                "max": round(max(interarrival), 2) if interarrival else 0.0,
            },
            "gateway_tag_counts": ws_result["gateway_tag_counts"],
        },
        "historian": {
            "rows_seen": hist_result["rows_seen"],
            "lag_ms": {
                "avg": round(statistics.fmean(hist_lag), 2) if hist_lag else 0.0,
                "p50": round(median(hist_lag), 2),
                "p95": round(pct(hist_lag, 0.95), 2),
                "max": round(max(hist_lag), 2) if hist_lag else 0.0,
            },
        },
        "status": status_result,
        "interval_truth": interval_result,
        "pass": {
            "ws_has_samples": ws_result["sample_count"] > 0,
            "historian_has_rows": hist_result["rows_seen"] > 0,
            "ws_p95_within_target": (report_ws_p95 := round(pct(ws_lag, 0.95), 2)) <= float(args.max_ws_p95_ms),
            "historian_p95_within_target": (report_hist_p95 := round(pct(hist_lag, 0.95), 2)) <= float(args.max_historian_p95_ms),
            "flicker_transitions_within_target": all(v <= int(args.max_flickers) for v in status_result["transitions"].values()),
            "interval_truth_no_mismatch": not bool(interval_result.get("mismatches")),
        },
    }

    report_path = os.path.abspath(args.report)
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(json.dumps({
        "ok": all(report["pass"].values()),
        "report": report_path,
        "ws_samples": ws_result["sample_count"],
        "historian_rows": hist_result["rows_seen"],
        "status_transitions": status_result["transitions"],
        "ws_p95_ms": report["ws"]["lag_ms"]["p95"],
        "historian_p95_ms": report["historian"]["lag_ms"]["p95"],
        "interval_mismatches": interval_result.get("mismatches", {}),
    }, indent=2))

    if not all(report["pass"].values()):
        sys.exit(2)


if __name__ == "__main__":
    main()
