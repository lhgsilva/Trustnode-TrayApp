# -*- coding: utf-8 -*-
"""Ask an ifm block, on YOUR network, exactly what it will and will not do.

Run this on the computer that talks to the block. It needs nothing from the
TrustNode app - no backend, no login, no gateway - so it works even when the
gateway shows RUNNING and collects nothing.

    python scripts/diagnose_ifm.py 192.168.1.250
    python scripts/diagnose_ifm.py 192.168.1.250 --port 8080
    python scripts/diagnose_ifm.py 192.168.1.250 --json report.json

It reports, in order: whether the IoT Core answers at all, what the block says
it is, which datapoints it offers, whether getdatamulti works and at what batch
size, and then reads every datapoint one by one with a timing. The last section
is a plain-language verdict with the settings to use.

Paste the output back to support - it is the fastest way to get a block that
will not read diagnosed, and it contains no credentials.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request

CANDIDATE_PORTS = [80, 8080, 443]


def _say(line=""):
    print(line, flush=True)


def _rule(title):
    _say()
    _say(title)
    _say("-" * len(title))


class Probe:
    def __init__(self, host, port, timeout, use_https, user, pw):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.use_https = use_https
        self.user = user
        self.pw = pw
        self._opener = None

    def base(self):
        scheme = "https" if self.use_https else "http"
        default = 443 if self.use_https else 80
        suffix = "" if int(self.port) == default else ":%d" % int(self.port)
        return "%s://%s%s" % (scheme, self.host, suffix)

    def opener(self):
        if self._opener is not None:
            return self._opener
        handlers = []
        if self.use_https:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        if self.user:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, self.base(), self.user, self.pw or "")
            handlers.append(urllib.request.HTTPBasicAuthHandler(mgr))
        self._opener = urllib.request.build_opener(*handlers)
        return self._opener

    def post(self, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base() + "/", data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        t0 = time.monotonic()
        with self.opener().open(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        return payload, (time.monotonic() - t0)

    def get(self, adr):
        req = urllib.request.Request(self.base() + adr, method="GET")
        t0 = time.monotonic()
        with self.opener().open(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        return payload, (time.monotonic() - t0)


def tcp_open(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def walk_adrs(node, path=""):
    """Every readable address in a gettree reply, whatever the nesting."""
    found = []
    if isinstance(node, dict):
        ident = node.get("identifier")
        subs = node.get("subs")
        here = path
        if isinstance(ident, str) and ident:
            here = path + "/" + ident
        if isinstance(subs, list):
            for s in subs:
                found += walk_adrs(s, here)
        elif isinstance(subs, dict):
            for key, val in subs.items():
                found += walk_adrs(val, here + "/" + str(key))
        else:
            if here:
                found.append(here)
        # a node that declares a getdata service is itself readable
        if isinstance(subs, list) and any(
                isinstance(s, dict) and s.get("identifier") == "getdata" for s in subs):
            found.append(here)
    return found


def main():
    ap = argparse.ArgumentParser(description="Diagnose an ifm IoT-Core block.")
    ap.add_argument("host", help="block IP, e.g. 192.168.1.250")
    ap.add_argument("--port", type=int, default=0, help="IoT Core port (default: probe 80/8080/443)")
    ap.add_argument("--https", action="store_true")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--max-reads", type=int, default=40,
                    help="how many datapoints to read individually")
    ap.add_argument("--json", default="", help="also write the raw findings here")
    args = ap.parse_args()

    report = {"host": args.host, "steps": {}}
    _say("TrustNode - ifm block diagnosis")
    _say("block: %s" % args.host)

    # ---------------------------------------------------------------- 1. TCP
    _rule("1. Can this computer reach the block?")
    ports = [args.port] if args.port else CANDIDATE_PORTS
    reachable = []
    for p in ports:
        ok = tcp_open(args.host, p)
        _say("   port %-5s %s" % (p, "OPEN" if ok else "closed / filtered"))
        if ok:
            reachable.append(p)
    report["steps"]["tcp"] = {"probed": ports, "open": reachable}
    if not reachable:
        _say()
        _say("   Nothing is listening. Before anything else:")
        _say("     - ping %s" % args.host)
        _say("     - confirm this PC is on the same subnet (ipconfig)")
        _say("     - a VPN will silently swallow this traffic - disconnect it")
        _say("     - the block's IoT Core may be disabled, or on another port")
        if args.json:
            open(args.json, "w", encoding="utf-8").write(json.dumps(report, indent=2))
        return 2

    port = args.port or reachable[0]
    probe = Probe(args.host, port, args.timeout, args.https, args.user, args.password)
    _say("   using port %d" % port)

    # ------------------------------------------------------------ 2. gettree
    _rule("2. What does the block say it is?")
    tree = {}
    try:
        payload, dt = probe.post({"code": "request", "cid": -1, "adr": "gettree"})
        tree = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        _say("   gettree answered in %.0f ms, code %s" % (dt * 1000, payload.get("code")))
        _say("   identifier: %s" % (tree.get("identifier") or "(none)"))
        text = json.dumps(tree)
        variant = "IO-Link master" if "iolinkmaster" in text else (
            "I/O module (digital in/out)" if '"io"' in text else "unknown")
        _say("   looks like: %s" % variant)
        report["steps"]["gettree"] = {"ok": True, "ms": round(dt * 1000),
                                      "identifier": tree.get("identifier"),
                                      "variant": variant, "bytes": len(text)}
    except Exception as exc:
        _say("   FAILED: %s" % exc)
        _say("   The port is open but this is not an ifm IoT Core endpoint,")
        _say("   or it needs credentials (--user / --password).")
        report["steps"]["gettree"] = {"ok": False, "error": str(exc)}
        if args.json:
            open(args.json, "w", encoding="utf-8").write(json.dumps(report, indent=2))
        return 2

    # ---------------------------------------------------------- 3. datapoints
    _rule("3. What does it offer to read?")
    adrs = []
    try:
        payload, dt = probe.post({"code": "request", "cid": -1, "adr": "querytree",
                                  "data": {"profile": "processdata"}})
        data = payload.get("data") or {}
        subs = data.get("subs") if isinstance(data, dict) else None
        if isinstance(subs, list):
            adrs = [str(s.get("adr")) for s in subs if isinstance(s, dict) and s.get("adr")]
        _say("   querytree(processdata): %d address(es) in %.0f ms" % (len(adrs), dt * 1000))
    except Exception as exc:
        _say("   querytree failed (%s) - falling back to walking gettree" % exc)
    if not adrs:
        adrs = sorted(set(walk_adrs(tree)))
        _say("   walked gettree: %d candidate address(es)" % len(adrs))
    adrs = [a if a.endswith("/getdata") else a + "/getdata" for a in adrs]
    for a in adrs[:12]:
        _say("     %s" % a)
    if len(adrs) > 12:
        _say("     ... and %d more" % (len(adrs) - 12))
    report["steps"]["datapoints"] = {"count": len(adrs), "addresses": adrs}
    if not adrs:
        _say()
        _say("   The block lists nothing readable. Check that inputs are")
        _say("   configured on the block itself (ifm moneo / LR DEVICE).")
        if args.json:
            open(args.json, "w", encoding="utf-8").write(json.dumps(report, indent=2))
        return 2

    # ------------------------------------------------------- 4. getdatamulti
    _rule("4. Does getdatamulti work, and how many at once?")
    multi = {}
    best = 0
    for size in (1, 4, 8, 16, 32):
        if size > len(adrs):
            break
        chunk = adrs[:size]
        try:
            payload, dt = probe.post({"code": "request", "cid": 1, "adr": "/getdatamulti",
                                      "data": {"datatosend": chunk}})
            items = payload.get("data") or {}
            got = len(items) if isinstance(items, dict) else 0
            ok = got > 0
            _say("   %2d address(es): %s  (%d returned, %.0f ms)"
                 % (size, "OK" if ok else "no data", got, dt * 1000))
            multi[size] = {"ok": ok, "returned": got, "ms": round(dt * 1000)}
            if ok:
                best = size
        except Exception as exc:
            _say("   %2d address(es): FAILED - %s" % (size, str(exc)[:70]))
            multi[size] = {"ok": False, "error": str(exc)[:200]}
    report["steps"]["getdatamulti"] = {"by_size": multi, "best": best}
    if not best:
        _say()
        _say("   This block will NOT batch reads. TrustNode falls back to")
        _say("   reading each value separately, in parallel - which is why the")
        _say("   collection interval matters on this block.")

    # -------------------------------------------------------- 5. single reads
    _rule("5. Reading each value individually")
    sample = adrs[: max(1, int(args.max_reads))]
    results = []
    slowest = 0.0
    ok_n = 0
    for a in sample:
        try:
            payload, dt = probe.get(a)
            code = int(payload.get("code") or 200)
            value = ((payload.get("data") or {}) or {}).get("value")
            slowest = max(slowest, dt)
            good = code < 400 and value is not None
            ok_n += 1 if good else 0
            results.append({"adr": a, "code": code, "value": value,
                            "ms": round(dt * 1000)})
            _say("   %-58s %-6s %s (%d ms)"
                 % (a[-58:], code, repr(value)[:22], dt * 1000))
        except Exception as exc:
            results.append({"adr": a, "error": str(exc)[:200]})
            _say("   %-58s FAILED %s" % (a[-58:], str(exc)[:40]))
    report["steps"]["single_reads"] = {"tried": len(sample), "ok": ok_n,
                                       "slowest_ms": round(slowest * 1000),
                                       "results": results}

    # ------------------------------------------------------------- 6. verdict
    _rule("6. Verdict")
    _say("   reachable on port %d ................ yes" % port)
    _say("   IoT Core answers gettree ........... yes")
    _say("   datapoints offered ................. %d" % len(adrs))
    _say("   getdatamulti ....................... %s"
         % ("yes, up to %d per request" % best if best
            else "NO - falls back to single reads"))
    _say("   values read individually ........... %d of %d" % (ok_n, len(sample)))
    _say("   slowest single read ................ %d ms" % round(slowest * 1000))

    _say()
    if ok_n == 0:
        _say("   The block is reachable and describes itself, but NOT ONE value")
        _say("   could be read. That points at the block's own configuration:")
        _say("   the ports/pins are not set as inputs, or the values live under")
        _say("   a different branch than the ones listed above.")
        _say("   Send this whole output back - the address list is what matters.")
    else:
        per_cycle = (len(adrs) / max(1, best)) if best else (len(adrs) / 8.0)
        est_s = per_cycle * max(slowest, 0.05)
        _say("   A cycle needs roughly %.1f s for all %d values." % (est_s, len(adrs)))
        suggested = 1000
        while suggested / 1000.0 < est_s * 1.5:
            suggested += 500
        _say("   Suggested gateway interval: %d ms" % max(1000, suggested))
        if ok_n < len(sample):
            _say("   %d value(s) did not read - those tags will show BAD."
                 % (len(sample) - ok_n))
            _say("   Deselect them, or fix them on the block.")

    if args.json:
        open(args.json, "w", encoding="utf-8").write(json.dumps(report, indent=2))
        _say()
        _say("   raw findings written to %s" % args.json)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
