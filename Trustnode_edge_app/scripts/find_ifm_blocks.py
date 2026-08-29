# -*- coding: utf-8 -*-
"""Find ifm IO-Link blocks on this computer's networks.

Answers one question: what is the IP of the block plugged into this machine?

It sweeps each local /24 for an open IoT-Core port, then asks every responder
`gettree`. Only a device that answers that is reported as a block, so a printer
or a NAS on port 80 will not be mistaken for one.

    python scripts/find_ifm_blocks.py                 # every local /24
    python scripts/find_ifm_blocks.py 192.168.1       # one subnet
    python scripts/find_ifm_blocks.py 192.168.1 --deep  # also probe 8080/443

Reads nothing and writes nothing on the device - `gettree` is a description
request. Safe to run against a live line.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import ipaddress
import json
import socket
import sys
import urllib.error
import urllib.request

IOT_PORTS = [80]
DEEP_PORTS = [80, 8080, 443]
# ifm electronic's OUI, as seen in a block's own gettree identifier
IFM_OUI = ("00-02-01", "00:02:01")


def local_subnets() -> list[str]:
    """Every /24 this machine has an address on, private ranges only."""
    out: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except Exception:
        infos = []
    seen = set()
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except Exception:
            continue
        if not addr.is_private or addr.is_loopback or addr.is_link_local:
            continue
        prefix = ".".join(ip.split(".")[:3])
        if prefix not in seen:
            seen.add(prefix)
            out.append(prefix)
    return out


def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def ask_gettree(host: str, port: int, timeout: float = 2.5) -> dict | None:
    """A block answers `gettree` with its whole device description."""
    scheme = "https" if port == 443 else "http"
    suffix = "" if port in (80, 443) else f":{port}"
    url = f"{scheme}://{host}{suffix}/"
    body = json.dumps({"code": "request", "cid": -1, "adr": "gettree"}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        if scheme == "https":
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def read_value(host: str, port: int, adr: str, timeout: float = 2.0):
    scheme = "https" if port == 443 else "http"
    suffix = "" if port in (80, 443) else f":{port}"
    try:
        req = urllib.request.Request(f"{scheme}://{host}{suffix}{adr}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        return ((payload.get("data") or {}) or {}).get("value")
    except Exception:
        return None


def describe(tree: dict) -> dict:
    text = json.dumps(tree)
    return {
        "identifier": str(tree.get("identifier") or ""),
        "kind": ("IO-Link master" if "iolinkmaster" in text
                 else ("I/O module (digital in/out)" if '"io"' in text else "ifm device")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Find ifm IO-Link blocks on the LAN.")
    ap.add_argument("subnet", nargs="?", default="",
                    help="e.g. 192.168.1 (default: every local /24)")
    ap.add_argument("--deep", action="store_true", help="also probe 8080 and 443")
    ap.add_argument("--timeout", type=float, default=0.25, help="per-host connect timeout")
    args = ap.parse_args()

    ports = DEEP_PORTS if args.deep else IOT_PORTS
    subnets = [args.subnet.strip().rstrip(".")] if args.subnet else local_subnets()
    if not subnets:
        print("  no private IPv4 subnet found on this machine")
        return 2

    print("TrustNode - ifm block finder")
    print("  subnets : %s" % ", ".join(s + ".0/24" for s in subnets))
    print("  ports   : %s" % ", ".join(str(p) for p in ports))
    print()

    found = []
    for prefix in subnets:
        hosts = [f"{prefix}.{i}" for i in range(1, 255)]
        open_hosts: list[tuple[str, int]] = []
        with futures.ThreadPoolExecutor(max_workers=128) as pool:
            jobs = {}
            for h in hosts:
                for p in ports:
                    jobs[pool.submit(port_open, h, p, args.timeout)] = (h, p)
            for fut in futures.as_completed(jobs):
                if fut.result():
                    open_hosts.append(jobs[fut])
        open_hosts.sort(key=lambda hp: tuple(int(x) for x in hp[0].split(".")))
        print("  %s.0/24 : %d host(s) answering on %s"
              % (prefix, len(open_hosts), "/".join(str(p) for p in ports)))

        # Only a device that answers gettree is an ifm block.
        with futures.ThreadPoolExecutor(max_workers=24) as pool:
            jobs = {pool.submit(ask_gettree, h, p): (h, p) for h, p in open_hosts}
            for fut in futures.as_completed(jobs):
                host, port = jobs[fut]
                tree = fut.result()
                if tree:
                    found.append((host, port, tree))

    print()
    if not found:
        print("  NO ifm BLOCK FOUND.")
        print()
        print("  Things worth checking, roughly in order:")
        print("    - a VPN will swallow LAN traffic. This machine has NordVPN")
        print("      adapters up; disconnect it and run this again.")
        print("    - the block must be on the same subnet as one of this")
        print("      machine's addresses, or reachable through a route.")
        print("    - the block's IoT Core may be on a non-default port: --deep")
        print("    - some blocks ship with DHCP off and a fixed 192.168.1.250.")
        return 2

    print("  FOUND %d ifm block(s):" % len(found))
    for host, port, tree in sorted(found, key=lambda f: tuple(int(x) for x in f[0].split("."))):
        info = describe(tree)
        product = read_value(host, port, "/deviceinfo/productcode/getdata")
        serial = read_value(host, port, "/deviceinfo/serialnumber/getdata")
        print()
        print("    IP address     : %s" % host)
        print("    IoT Core port  : %s" % port)
        print("    looks like     : %s" % info["kind"])
        if product:
            print("    product code   : %s" % product)
        if serial:
            print("    serial number  : %s" % serial)
        ident = info["identifier"]
        if ident:
            note = "  (ifm MAC)" if any(ident.upper().startswith(o) for o in IFM_OUI) else ""
            print("    identifier     : %s%s" % (ident, note))
        print("    next step      : python scripts/diagnose_ifm.py %s%s"
              % (host, "" if port == 80 else " --port %d" % port))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
