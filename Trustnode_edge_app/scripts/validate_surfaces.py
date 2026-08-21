"""Surface checks for the release gate (operator 2026-08-21, plan Phase 0.2).

Proves, against the RUNNING build, that the browser surfaces the edge serves
and the access policy around them behave:

  loopback (always):
    * /trustnode/login/ and the three landings answer 200
    * /trustnode/{full,client,lite}/app/ redirect (302) to the login page
      without a session and answer 200 with an admin session cookie
      (= the UI bundles made it into the build and the static guard is on)
    * GET /api/lan-sharing/status 200 (admin) with the Remote Access fields
    * /api/lite-local/bootstrap + /reports 200 for an admin
    * the Backup & Retention page's API (status/options/policies/runs)
  remote (only when Remote Access is ON and a LAN IP is known; the checks
  run over HTTP, or over HTTPS when the site is HTTPS-only):
    * every enabled listener answers /api/health
    * the certificate download works
    * admin from the LAN: full bundle 200, config PUT allowed
    * a temporary viewer from the LAN: GET allowed, mutation 403, full
      bundle 403, Local View bundle 200, token refused after revoke (401),
      lockout after repeated failures (423), master default password blocked

Standalone: python scripts/validate_surfaces.py  (exit 0 PASS / 2 FAIL)
Imported by validate_full_12h.py: run() -> (ok, lines, metrics).
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("VAL_API", "http://127.0.0.1:8000")
LOGIN = {"username": os.environ.get("VAL_USER", "admin-mari"), "password": os.environ.get("VAL_PASS", "Limerick2019*")}
VIEWER = ("val-surface-viewer", "viewer-pass-2026")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _call(method, url, token=None, body=None, timeout=12, raw=False, cookies=None, follow=True):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if cookies:
        h["Cookie"] = cookies
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    handlers = [urllib.request.HTTPSHandler(context=ctx)] + ([] if follow else [_NoRedirect()])
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            txt = r.read()
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            if raw:
                return r.status, txt, hdrs
            try:
                return r.status, json.loads(txt.decode()), hdrs
            except Exception:
                return r.status, txt[:200], hdrs
    except urllib.error.HTTPError as e:
        txt = e.read()
        hdrs = {k.lower(): v for k, v in e.headers.items()}
        try:
            return e.code, json.loads(txt.decode()), hdrs
        except Exception:
            return e.code, txt[:200], hdrs
    except Exception as e:
        return 0, str(e), {}


def _login(base, user, pw):
    st, body, hdrs = _call("POST", f"{base}/api/auth/login", body={"username": user, "password": pw})
    return st, (body.get("token") if isinstance(body, dict) else None), body, hdrs


def run(remote: bool = True) -> tuple[bool, list[str], dict]:
    L: list[str] = []
    ok_all = True
    metrics: dict = {"checks": 0, "failed": 0}

    def chk(name, ok, detail=""):
        nonlocal ok_all
        metrics["checks"] += 1
        if not ok:
            metrics["failed"] += 1
            ok_all = False
        L.append(f"  {name:44s}: {'PASS' if ok else 'FAIL'}{(' (' + str(detail) + ')') if detail else ''}")

    st, admin, body, hdrs = _login(API, LOGIN["username"], LOGIN["password"])
    chk("admin login (loopback)", st == 200 and bool(admin), f"status={st}")
    if not admin:
        return False, L, metrics
    chk("login sets session cookie", "tn_session" in str(hdrs.get("set-cookie") or ""))

    # loopback surfaces
    for path in ("/trustnode/login/", "/trustnode/full/", "/trustnode/client/", "/trustnode/lite/"):
        st, _, _ = _call("GET", f"{API}{path}", raw=True)
        chk(f"{path} public 200", st == 200, f"status={st}")
    for surf in ("full", "client", "lite"):
        st, _, h = _call("GET", f"{API}/trustnode/{surf}/app/", raw=True, follow=False)
        chk(f"/trustnode/{surf}/app/ no session -> 302 login", st == 302 and "/trustnode/login/" in str(h.get("location") or ""), f"status={st}")
        st, _, _ = _call("GET", f"{API}/trustnode/{surf}/app/", raw=True, cookies=f"tn_session={admin}", follow=False)
        chk(f"/trustnode/{surf}/app/ admin cookie -> 200", st == 200, f"status={st}")

    st, status, _ = _call("GET", f"{API}/api/lan-sharing/status", token=admin)
    chk("lan-sharing status (admin)", st == 200 and isinstance(status, dict) and "view_urls" in status and "https" in status, f"status={st}")
    st, b, _ = _call("GET", f"{API}/api/lite-local/bootstrap", token=admin)
    chk("lite-local bootstrap (admin)", st == 200, f"status={st}")
    st, b, _ = _call("GET", f"{API}/api/lite-local/reports?limit=3", token=admin)
    chk("lite-local reports (admin)", st == 200, f"status={st}")
    st, b, _ = _call("GET", f"{API}/api/lan-sharing/status")
    chk("tray loopback status without token", st == 200, f"status={st}")

    # Backup & Retention page (2026-08-21): every call the page makes on load.
    # The page died on a legacy-shaped run row, and the API prefix is
    # /api/app-store/retention/v2 — probing /api/retention/v2 404s and looks
    # like the feature is gone.
    RET = f"{API}/api/app-store/retention/v2"
    _t0 = time.time()
    st, rs, _ = _call("GET", f"{RET}/status", token=admin)
    _ret_ms = (time.time() - _t0) * 1000.0
    chk("retention status (admin)",
        st == 200 and isinstance(rs, dict) and isinstance(rs.get("status"), dict),
        f"status={st}")
    # The page fetches this on load with a 12 s client timeout; at 9 M rows the
    # unoptimised version took 6.5 s and lost the race, rendering 0 B / 0
    # readings. Keep it well inside the budget.
    chk("retention status answers < 5s", _ret_ms < 5000, f"{_ret_ms:.0f}ms")
    metrics["retention_status_ms"] = round(_ret_ms)
    if st == 200:
        eng = (rs.get("status") or {}).get("engine") or {}
        chk("retention engine thread running", bool(eng.get("running")), eng)
    st, ro, _ = _call("GET", f"{RET}/options", token=admin)
    chk("retention options (admin)", st == 200 and bool((ro or {}).get("resolutions")), f"status={st}")
    st, rp, _ = _call("GET", f"{RET}/policies", token=admin)
    chk("retention policies (admin)", st == 200 and "policies" in (rp or {}), f"status={st}")
    st, rr, _ = _call("GET", f"{RET}/runs?limit=5", token=admin)
    chk("retention runs history (admin)", st == 200 and "runs" in (rr or {}), f"status={st}")
    st, bk, _ = _call("GET", f"{API}/api/app-store/backups/v2", token=admin)
    chk("backups list (admin)", st in (200, 404), f"status={st}")

    if not remote or not isinstance(status, dict) or not status.get("running"):
        L.append("  (remote checks skipped: Remote Access is OFF)")
        return ok_all, L, metrics
    ips = status.get("ips") or []
    http_port = int(status.get("lan_port") or 0)
    https_port = int(status.get("https_port") or 0)
    metrics.update(lan_ip=ips[0] if ips else "", http_port=http_port, https_port=https_port,
                   licensed=status.get("licensed"), rbac_mode=status.get("rbac_mode"))
    if not ips:
        L.append("  (remote checks skipped: no LAN IP on this machine)")
        return ok_all, L, metrics
    if not http_port and not https_port:
        # Remote Access is ON but neither listener bound: that is a failure, not
        # something to skip over (2026-08-21: the HTTPS-only site silently
        # skipped every remote check because this only looked at the HTTP port).
        chk("a LAN listener is bound", False, "no HTTP and no HTTPS port")
        return ok_all, L, metrics
    # Run the remote half over whichever transport the operator left enabled;
    # an HTTPS-only site must be exercised exactly like an HTTP one.
    LAN = (f"http://{ips[0]}:{http_port}" if http_port
           else f"https://{ips[0]}:{https_port}")
    L.append(f"  (remote checks over {LAN})")
    metrics["remote_base"] = LAN
    st, pem, _ = _call("GET", f"{API}/api/lan-sharing/certificate", raw=True)
    chk("certificate download", st == 200 and b"BEGIN CERTIFICATE" in pem, f"status={st}")
    if https_port:
        st, _, _ = _call("GET", f"https://{ips[0]}:{https_port}/api/health")
        chk("https listener /api/health", st == 200, f"status={st}")
    if http_port:
        st, _, _ = _call("GET", f"http://{ips[0]}:{http_port}/api/health")
        chk("http LAN listener /api/health", st == 200, f"status={st}")
    else:
        L.append("  (HTTP listener intentionally off: HTTPS-only mode)")

    st, admin_r, body, _ = _login(LAN, LOGIN["username"], LOGIN["password"])
    chk("admin login from LAN", st == 200 and bool(admin_r), f"status={st}")
    if admin_r:
        pl = json.loads(base64.urlsafe_b64decode(admin_r.split(".")[1] + "==").decode())
        chk("LAN session TTL 4h", (pl["exp"] - pl["iat"]) == 4 * 3600, f"ttl={pl['exp'] - pl['iat']}")
        st, _, _ = _call("GET", f"{LAN}/trustnode/full/app/", raw=True, cookies=f"tn_session={admin_r}", follow=False)
        chk("LAN admin loads full bundle", st == 200, f"status={st}")
        st, b, _ = _call("PUT", f"{LAN}/api/lan-sharing/config", token=admin_r, body={})
        chk("LAN admin configuration allowed", st == 200, f"status={st} {b if st != 200 else ''}")

    st, b, _ = _call("POST", f"{API}/api/control-plane/users", token=admin,
                     body={"username": VIEWER[0], "password": VIEWER[1], "role": "viewer", "status": "active",
                           "permissions": {"access_client": True}, "modules": []})
    chk("temp viewer created", st in (200, 201), f"status={st}")
    time.sleep(0.8)
    st, vtok, body, _ = _login(LAN, *VIEWER)
    chk("viewer login from LAN", st == 200 and bool(vtok), f"status={st}")
    if vtok:
        st, _, _ = _call("GET", f"{LAN}/api/plc/gateways/status", token=vtok)
        chk("viewer read allowed", st == 200, f"status={st}")
        st, b, _ = _call("POST", f"{LAN}/api/plc/gateways/stop", token=vtok, body={"gateway_id": "does-not-exist"})
        chk("viewer mutation refused (403)", st == 403, f"status={st}")
        st, _, _ = _call("GET", f"{LAN}/trustnode/full/app/", raw=True, token=vtok, follow=False)
        chk("viewer full bundle refused (403)", st == 403, f"status={st}")
        st, _, _ = _call("GET", f"{LAN}/trustnode/client/app/", raw=True, token=vtok, follow=False)
        chk("viewer Local View bundle (access_client)", st == 200, f"status={st}")
        st, b, _ = _call("POST", f"{API}/api/lan-sharing/sessions/revoke", token=admin, body={"username": VIEWER[0]})
        chk("revoke viewer", st == 200, f"status={st}")
        st, b, _ = _call("GET", f"{LAN}/api/plc/gateways/status", token=vtok)
        chk("revoked token refused (401)", st == 401, f"status={st}")
    last = 0
    for _ in range(5):  # 5th failure trips the lockout; keeps total logins under the per-IP limiter (10/min)
        last, _, _, _ = _login(LAN, VIEWER[0], "definitely-wrong-pw")
    chk("lockout after repeated failures (423)", last == 423, f"last={last}")
    _call("POST", f"{API}/api/auth/unlock", token=admin, body={"username": VIEWER[0]})
    st, _, _, _ = _login(LAN, "admin", "admin")
    chk("master default password blocked from LAN", st in (401, 403), f"status={st}")
    _call("DELETE", f"{API}/api/control-plane/users/{VIEWER[0]}", token=admin)
    return ok_all, L, metrics


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ok, lines, metrics = run(remote=True)
    print("[SURFACES]")
    print("\n".join(lines))
    print(f"  SURFACES: {'PASS' if ok else 'FAIL'} ({metrics.get('checks', 0) - metrics.get('failed', 0)}/{metrics.get('checks', 0)})")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
