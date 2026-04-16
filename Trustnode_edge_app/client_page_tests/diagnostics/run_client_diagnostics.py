#!/usr/bin/env python3
import argparse
import json
import os
import statistics
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

URLS = [
    "https://trustnode.lsapps.app/client/client_test.html",
    "https://trustnode.lsapps.app/client/client_test.php",
    "https://trustnode.lsapps.app/client/client_test_db_rest.html",
    "https://trustnode.lsapps.app/client/client_test_db_php.html",
    "https://trustnode.lsapps.app/client/client_test_db.php",
]


@dataclass
class BenchStats:
    url: str
    runs: int
    ok: int
    fail: int
    status_codes: Dict[str, int]
    min_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    avg_ms: float
    avg_bytes: float
    content_type: str
    title: str
    mode: str



def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    s = s.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_mode(text: str, url: str) -> str:
    txt = text[:250000]
    if "dbproxy=" in txt or "TRUSTNODE_DB_DSN" in txt:
        return "PHP direct DB + API proxy"
    if "window.__TN_DB_URL" in txt and "rest/v1" in txt:
        return "Browser direct DB REST + API fallback"
    if "?proxy=" in txt and url.endswith(".php"):
        return "PHP API proxy"
    if "__TN_PROXY_BASE" in txt:
        return "Browser direct API"
    return "Unknown"


def extract_title(text: str) -> str:
    low = text.lower()
    i = low.find("<title>")
    j = low.find("</title>")
    if i >= 0 and j > i:
        return text[i + 7 : j].strip()
    return ""


def bench_url(session: requests.Session, url: str, runs: int, timeout: int) -> BenchStats:
    durations = []
    sizes = []
    ok = 0
    fail = 0
    status_codes: Dict[str, int] = {}
    content_type = ""
    title = ""
    mode = ""

    for idx in range(runs):
        t0 = time.perf_counter()
        try:
            resp = session.get(url, timeout=timeout)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            durations.append(elapsed_ms)
            sizes.append(len(resp.content))
            code = str(resp.status_code)
            status_codes[code] = status_codes.get(code, 0) + 1
            if 200 <= resp.status_code < 400:
                ok += 1
            else:
                fail += 1
            if idx == 0:
                content_type = resp.headers.get("Content-Type", "")
                text = resp.text
                title = extract_title(text)
                mode = classify_mode(text, url)
        except Exception:
            fail += 1

    if durations:
        sd = sorted(durations)
        p95_index = max(0, min(len(sd) - 1, int(round(len(sd) * 0.95)) - 1))
        min_ms = sd[0]
        p50_ms = statistics.median(sd)
        p95_ms = sd[p95_index]
        max_ms = sd[-1]
        avg_ms = statistics.fmean(sd)
    else:
        min_ms = p50_ms = p95_ms = max_ms = avg_ms = 0.0

    avg_bytes = statistics.fmean(sizes) if sizes else 0.0

    return BenchStats(
        url=url,
        runs=runs,
        ok=ok,
        fail=fail,
        status_codes=status_codes,
        min_ms=round(min_ms, 2),
        p50_ms=round(p50_ms, 2),
        p95_ms=round(p95_ms, 2),
        max_ms=round(max_ms, 2),
        avg_ms=round(avg_ms, 2),
        avg_bytes=round(avg_bytes, 2),
        content_type=content_type,
        title=title,
        mode=mode,
    )


def login(base: str, username: str, password: str, timeout: int) -> Optional[str]:
    try:
        r = requests.post(
            f"{base.rstrip('/')}/api/auth/login",
            json={"username": username, "password": password},
            timeout=timeout,
        )
        if not (200 <= r.status_code < 300):
            return None
        j = r.json()
        return j.get("token")
    except Exception:
        return None


def auth_get(base: str, token: str, path: str, timeout: int) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(
            f"{base.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        if not (200 <= r.status_code < 300):
            return None
        return r.json()
    except Exception:
        return None


def collect_sync_diagnostics(local_api: str, cloud_api: str, username: str, password: str, timeout: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    local_token = login(local_api, username, password, timeout)
    cloud_token = login(cloud_api, username, password, timeout)

    out["local_login_ok"] = bool(local_token)
    out["cloud_login_ok"] = bool(cloud_token)

    if local_token:
        out["local_inspector"] = auth_get(local_api, local_token, "/api/app-store/inspector", timeout)
        out["local_live_1"] = auth_get(local_api, local_token, "/api/app-store/live?limit=1", timeout)
        out["local_hist_1"] = auth_get(local_api, local_token, "/api/app-store/historian?limit=1", timeout)

    if cloud_token:
        out["cloud_inspector"] = auth_get(cloud_api, cloud_token, "/api/app-store/inspector", timeout)
        out["cloud_live_1"] = auth_get(cloud_api, cloud_token, "/api/app-store/live?limit=1", timeout)
        out["cloud_hist_1"] = auth_get(cloud_api, cloud_token, "/api/app-store/historian?limit=1", timeout)

    def _rows(doc: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = (doc or {}).get("rows")
        return rows if isinstance(rows, list) else []

    def _key(row: Dict[str, Any]) -> str:
        return f"{row.get('gateway_id') or row.get('gateway_name') or ''}::{row.get('tag') or ''}"

    lags = []
    selected_key = ""
    if local_token and cloud_token:
        local_seed = auth_get(local_api, local_token, "/api/app-store/live?limit=400", timeout)
        cloud_seed = auth_get(cloud_api, cloud_token, "/api/app-store/live?limit=400", timeout)
        local_keys = {_key(r) for r in _rows(local_seed) if _key(r)}
        cloud_keys = {_key(r) for r in _rows(cloud_seed) if _key(r)}
        common = sorted(local_keys & cloud_keys)
        # Prefer a high-signal PLC/meter stream when possible.
        preferred = [
            k
            for k in common
            if ("simdint" in k.lower() or "simreal" in k.lower() or "power_meter" in k.lower() or "energy" in k.lower())
        ]
        selected_key = preferred[0] if preferred else (common[0] if common else "")
        out["live_lag_stream_key"] = selected_key
        for _ in range(10):
            probe_limit = 400 if selected_key else 1
            l = auth_get(local_api, local_token, f"/api/app-store/live?limit={probe_limit}", timeout)
            c = auth_get(cloud_api, cloud_token, f"/api/app-store/live?limit={probe_limit}", timeout)
            lts = None
            cts = None
            try:
                l_rows = _rows(l)
                c_rows = _rows(c)
                if selected_key:
                    l_match = next((r for r in l_rows if _key(r) == selected_key), None)
                    c_match = next((r for r in c_rows if _key(r) == selected_key), None)
                else:
                    l_match = l_rows[0] if l_rows else None
                    c_match = c_rows[0] if c_rows else None
                lts = parse_ts((l_match or {}).get("ts"))
                cts = parse_ts((c_match or {}).get("ts"))
            except Exception:
                pass
            if lts and cts:
                lags.append((lts - cts).total_seconds())
            time.sleep(1.0)
    out["live_lag_seconds_samples_local_minus_cloud"] = lags
    if lags:
        s = sorted(lags)
        out["live_lag_summary_seconds"] = {
            "min": round(s[0], 3),
            "p50": round(statistics.median(s), 3),
            "p95": round(s[max(0, min(len(s) - 1, int(round(len(s) * 0.95)) - 1))], 3),
            "max": round(s[-1], 3),
            "avg": round(statistics.fmean(s), 3),
        }

    return out


def _extract_rows(doc: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(doc, dict):
        return []
    rows = doc.get("rows")
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def _row_key(row: Dict[str, Any]) -> str:
    return f"{row.get('gateway_id') or row.get('gateway_name') or ''}::{row.get('tag') or ''}"


def _median_interval_seconds(rows: List[Dict[str, Any]]) -> Optional[float]:
    ts = []
    for r in rows:
        dt = parse_ts(str(r.get("ts") or ""))
        if dt:
            ts.append(dt.timestamp())
    if len(ts) < 3:
        return None
    ts = sorted(ts)
    deltas = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
    if not deltas:
        return None
    return round(statistics.median(deltas), 3)


def _latest_lag_seconds(local_rows: List[Dict[str, Any]], cloud_rows: List[Dict[str, Any]]) -> Optional[float]:
    if not local_rows or not cloud_rows:
        return None
    l = parse_ts(str(local_rows[0].get("ts") or ""))
    c = parse_ts(str(cloud_rows[0].get("ts") or ""))
    if not l or not c:
        return None
    return round((l - c).total_seconds(), 3)


def _value_consistency(local_rows: List[Dict[str, Any]], cloud_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    local_by_ts = {}
    for r in local_rows:
        k = str(r.get("ts") or "")
        if not k:
            continue
        local_by_ts[k] = r.get("value")
    cloud_by_ts = {}
    for r in cloud_rows:
        k = str(r.get("ts") or "")
        if not k:
            continue
        cloud_by_ts[k] = r.get("value")
    overlap = sorted(set(local_by_ts.keys()) & set(cloud_by_ts.keys()))
    # Retry with second-bucket normalized timestamps because cloud/local often
    # use different sub-second precision.
    if not overlap:
        local_sec = {}
        cloud_sec = {}
        for k, v in local_by_ts.items():
            dt = parse_ts(k)
            if dt:
                local_sec[dt.strftime("%Y-%m-%dT%H:%M:%S")] = v
        for k, v in cloud_by_ts.items():
            dt = parse_ts(k)
            if dt:
                cloud_sec[dt.strftime("%Y-%m-%dT%H:%M:%S")] = v
        overlap = sorted(set(local_sec.keys()) & set(cloud_sec.keys()))
        if overlap:
            local_by_ts = local_sec
            cloud_by_ts = cloud_sec
    if not overlap:
        return {"overlap_rows": 0, "equal_rows": 0, "mismatch_rows": 0, "equal_ratio": None}
    equal = 0
    mismatch = 0
    for ts in overlap:
        lv = local_by_ts.get(ts)
        cv = cloud_by_ts.get(ts)
        try:
            lf = float(lv)
            cf = float(cv)
            if abs(lf - cf) <= 1e-6:
                equal += 1
            else:
                mismatch += 1
        except Exception:
            if str(lv) == str(cv):
                equal += 1
            else:
                mismatch += 1
    return {
        "overlap_rows": len(overlap),
        "equal_rows": equal,
        "mismatch_rows": mismatch,
        "equal_ratio": round(equal / len(overlap), 4) if overlap else None,
    }


def _pick_stream_candidates(local_rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    ordered = sorted(local_rows, key=lambda r: str(r.get("ts") or ""), reverse=True)
    for r in ordered:
        key = _row_key(r)
        if not key or key in seen:
            continue
        gid = str(r.get("gateway_id") or r.get("gateway_name") or "").strip()
        tag = str(r.get("tag") or "").strip()
        if not gid or not tag:
            continue
        is_power = ("power" in gid.lower()) or ("energy" in tag.lower())
        if is_power and not any(("power" in x["gateway_id"].lower()) or ("energy" in x["tag"].lower()) for x in out):
            out.append({"gateway_id": gid, "tag": tag})
            seen.add(key)
        elif (not is_power) and not any(("power" not in x["gateway_id"].lower()) and ("energy" not in x["tag"].lower()) for x in out):
            out.append({"gateway_id": gid, "tag": tag})
            seen.add(key)
        if len(out) >= 4:
            break
    if len(out) < 4:
        for r in ordered:
            key = _row_key(r)
            if not key or key in seen:
                continue
            gid = str(r.get("gateway_id") or r.get("gateway_name") or "").strip()
            tag = str(r.get("tag") or "").strip()
            if not gid or not tag:
                continue
            out.append({"gateway_id": gid, "tag": tag})
            seen.add(key)
            if len(out) >= 4:
                break
    return out


def collect_stream_consistency(local_api: str, cloud_api: str, username: str, password: str, timeout: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"streams": []}
    local_token = login(local_api, username, password, timeout)
    cloud_token = login(cloud_api, username, password, timeout)
    if not local_token or not cloud_token:
        out["error"] = "login_failed"
        return out

    local_live = auth_get(local_api, local_token, "/api/app-store/live?limit=300", timeout)
    seed_rows = _extract_rows(local_live)
    candidates = _pick_stream_candidates(seed_rows)
    out["candidates"] = candidates
    if not candidates:
        return out

    for c in candidates:
        gid = c["gateway_id"]
        tag = c["tag"]
        q = f"?limit=180&gateway_id={requests.utils.quote(gid)}&tag={requests.utils.quote(tag)}"
        l_hist = auth_get(local_api, local_token, f"/api/app-store/historian{q}", timeout) or {}
        c_hist = auth_get(cloud_api, cloud_token, f"/api/app-store/historian{q}", timeout) or {}
        l_rows = _extract_rows(l_hist)
        c_rows = _extract_rows(c_hist)
        stream = {
            "gateway_id": gid,
            "tag": tag,
            "local_rows": len(l_rows),
            "cloud_rows": len(c_rows),
            "latest_lag_seconds_local_minus_cloud": _latest_lag_seconds(l_rows, c_rows),
            "local_median_interval_seconds": _median_interval_seconds(l_rows),
            "cloud_median_interval_seconds": _median_interval_seconds(c_rows),
            "value_consistency": _value_consistency(l_rows, c_rows),
        }
        out["streams"].append(stream)
    return out


def summarize_recommendation(bench: List[BenchStats]) -> List[str]:
    lines = []
    scored = sorted(bench, key=lambda x: (x.fail, x.p95_ms, x.avg_bytes))
    lines.append("Ranking (lower fail/p95/bytes is better for scale):")
    for i, b in enumerate(scored, start=1):
        lines.append(f"{i}. {b.url} | mode={b.mode} | fail={b.fail}/{b.runs} | p95={b.p95_ms} ms | avg_bytes={b.avg_bytes}")
    return lines


def write_markdown(
    report_path: Path,
    bench: List[BenchStats],
    sync: Dict[str, Any],
    stream_consistency: Dict[str, Any],
    playwright_data: Optional[Dict[str, Any]],
) -> None:
    lines: List[str] = []
    lines.append("# Client Pages Diagnostics Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append("")
    lines.append("## URL Performance")
    lines.append("")
    for b in bench:
        lines.append(f"### {b.url}")
        lines.append(f"- Mode: `{b.mode}`")
        lines.append(f"- Title: `{b.title}`")
        lines.append(f"- Content-Type: `{b.content_type}`")
        lines.append(f"- Success: `{b.ok}/{b.runs}`")
        lines.append(f"- Failures: `{b.fail}`")
        lines.append(f"- Status codes: `{b.status_codes}`")
        lines.append(f"- Latency ms: min `{b.min_ms}`, p50 `{b.p50_ms}`, p95 `{b.p95_ms}`, max `{b.max_ms}`, avg `{b.avg_ms}`")
        lines.append(f"- Avg response size bytes: `{b.avg_bytes}`")
        lines.append("")

    lines.append("## Edge/Cloud Sync Diagnostics")
    lines.append("")
    lines.append(f"- Local login ok: `{sync.get('local_login_ok')}`")
    lines.append(f"- Cloud login ok: `{sync.get('cloud_login_ok')}`")

    def row_from(doc: Optional[Dict[str, Any]]) -> str:
        if not doc:
            return "n/a"
        rows = doc.get("rows") or []
        if not rows:
            return "no rows"
        r0 = rows[0]
        return f"ts={r0.get('ts')} gateway={r0.get('gateway_name') or r0.get('gateway_id')} tag={r0.get('tag')} value={r0.get('value')}"

    lines.append(f"- Local live latest: `{row_from(sync.get('local_live_1'))}`")
    lines.append(f"- Cloud live latest: `{row_from(sync.get('cloud_live_1'))}`")
    lines.append(f"- Local historian latest: `{row_from(sync.get('local_hist_1'))}`")
    lines.append(f"- Cloud historian latest: `{row_from(sync.get('cloud_hist_1'))}`")

    local_ins = ((sync.get("local_inspector") or {}).get("inspector") or {})
    cloud_ins = ((sync.get("cloud_inspector") or {}).get("inspector") or {})
    if local_ins:
        ds = local_ins.get("data_sync") or {}
        so = local_ins.get("sync_outbox_status") or {}
        lines.append(f"- Local outbox pending/failed/sent: `{so.get('pending')}/{so.get('failed')}/{so.get('sent')}`")
        lines.append(f"- Local data_sync last_error: `{ds.get('last_data_error')}`")
        lines.append(f"- Local backlog hist/logs: `{ds.get('historian_backlog')}/{ds.get('logs_backlog')}`")
    if cloud_ins:
        ds = cloud_ins.get("data_sync") or {}
        so = cloud_ins.get("sync_outbox_status") or {}
        lines.append(f"- Cloud outbox pending/failed/sent: `{so.get('pending')}/{so.get('failed')}/{so.get('sent')}`")
        lines.append(f"- Cloud data_sync last_error: `{ds.get('last_data_error')}`")

    lag_summary = sync.get("live_lag_summary_seconds")
    if lag_summary:
        lines.append(f"- Live lag local-cloud seconds (local_ts - cloud_ts): `{lag_summary}`")

    lines.append("")
    lines.append("## Stream Consistency (PLC + Meter Candidates)")
    lines.append("")
    if stream_consistency.get("error"):
        lines.append(f"- Error: `{stream_consistency.get('error')}`")
    else:
        lines.append(f"- Candidate streams tested: `{len(stream_consistency.get('streams') or [])}`")
        for s in stream_consistency.get("streams") or []:
            lines.append(f"### {s.get('gateway_id')} :: {s.get('tag')}")
            lines.append(f"- Local rows: `{s.get('local_rows')}` | Cloud rows: `{s.get('cloud_rows')}`")
            lines.append(f"- Latest lag seconds (local-cloud): `{s.get('latest_lag_seconds_local_minus_cloud')}`")
            lines.append(
                f"- Median interval seconds: local `{s.get('local_median_interval_seconds')}` | cloud `{s.get('cloud_median_interval_seconds')}`"
            )
            lines.append(f"- Value consistency: `{s.get('value_consistency')}`")
            lines.append("")

    lines.append("")
    lines.append("## Runtime Network Profile (Playwright)")
    lines.append("")
    if playwright_data:
        for item in playwright_data.get("results", []):
            lines.append(f"### {item.get('url')}")
            lines.append(f"- Login attempted: `{item.get('login_attempted')}` | success: `{item.get('login_success')}`")
            lines.append(f"- Requests captured: `{item.get('request_count')}` | failures: `{item.get('failed_request_count')}`")
            lines.append(f"- Approx response bytes: `{item.get('response_bytes')}`")
            lines.append(f"- WebSocket connections: `{item.get('websocket_count')}`")
            lines.append(f"- Top paths: `{item.get('top_paths')}`")
            lines.append("")
    else:
        lines.append("- Playwright profile not found (run `network_profile_playwright.mjs` to include it).")
        lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.extend(f"- {x}" for x in summarize_recommendation(bench))
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- `client_test_db_rest.html` and `client_test_db_php.html` are browser direct-DB variants; they require DB key in browser and increase exposure risk.")
    lines.append("- `client_test_db.php` keeps DB credentials server-side and is safer than browser direct-DB while still reducing backend API roundtrips.")
    lines.append("- For production multi-tenant scale, prefer API or PHP server-proxy variants with WebSocket + bounded polling.")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Trustnode client page diagnostics")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--username", default=os.getenv("TRUSTNODE_DIAG_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("TRUSTNODE_DIAG_PASS", "admin"))
    parser.add_argument("--local-api", default=os.getenv("TRUSTNODE_LOCAL_API", "http://127.0.0.1:8000"))
    parser.add_argument("--cloud-api", default=os.getenv("TRUSTNODE_CLOUD_API", "https://trustnode.lsapps.app"))
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "output"))
    parser.add_argument("--playwright-json", default=str(Path(__file__).resolve().parent / "output" / "playwright_profile.json"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"Cache-Control": "no-store"})

    bench = [bench_url(session, u, args.runs, args.timeout) for u in URLS]
    sync = collect_sync_diagnostics(args.local_api, args.cloud_api, args.username, args.password, args.timeout)
    stream_consistency = collect_stream_consistency(
        args.local_api, args.cloud_api, args.username, args.password, args.timeout
    )

    playwright_data = None
    pj = Path(args.playwright_json)
    if pj.exists():
        try:
            playwright_data = json.loads(pj.read_text(encoding="utf-8"))
        except Exception:
            playwright_data = None

    raw = {
        "bench": [asdict(x) for x in bench],
        "sync": sync,
        "stream_consistency": stream_consistency,
        "playwright": playwright_data,
    }

    (out_dir / "client_diagnostics_raw.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    write_markdown(out_dir / "CLIENT_DIAGNOSTICS_REPORT.md", bench, sync, stream_consistency, playwright_data)
    print(f"Diagnostics written to: {out_dir}")


if __name__ == "__main__":
    main()
