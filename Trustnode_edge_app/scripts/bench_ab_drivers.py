"""Benchmark pycomm3 vs pylogix against the live AB sim.

Reads the SAME 32 tags via both drivers, 60 cycles each, prints per-
cycle timing + min/max/avg + count of cycles over the 5s stall
threshold. Standalone — does NOT touch the running edge app.

Usage:
    python scripts/bench_ab_drivers.py [--ip 192.168.10.240] [--cycles 60]
"""
from __future__ import annotations
import argparse, statistics, sys, time

TAGS = [
    "SimDINT[0]","SimDINT[1]","SimDINT[2]","SimDINT[3]","SimDINT[4]",
    "SimDINT[5]","SimDINT[6]","SimDINT[7]","SimDINT[8]","SimDINT[9]",
    "SimREAL[0]","SimREAL[1]","SimREAL[2]","SimREAL[3]","SimREAL[4]",
    "SimREAL[5]","SimREAL[6]","SimREAL[7]","SimREAL[8]","SimREAL[9]",
    "Start",
    "Test_Tags[0]","Test_Tags[1]","Test_Tags[2]","Test_Tags[3]","Test_Tags[4]",
    "Test_Tags[5]","Test_Tags[6]","Test_Tags[7]","Test_Tags[8]","Test_Tags[9]",
    "temp",
]


def bench_pycomm3(ip: str, cycles: int, batch_size: int):
    from pycomm3 import LogixDriver
    print(f"\n=== pycomm3 (batch_size={batch_size}) ===")
    timings = []
    stalls = 0
    errors = 0
    plc = LogixDriver(ip, init_tags=True, init_program_tags=False)
    plc._cfg["socket_timeout"] = 2.0
    plc.open()
    try:
        # Warm-up read
        plc.read(TAGS[0])
        for i in range(cycles):
            t0 = time.monotonic()
            try:
                if batch_size >= len(TAGS):
                    plc.read(*TAGS)
                else:
                    for j in range(0, len(TAGS), batch_size):
                        plc.read(*TAGS[j:j + batch_size])
                dt = time.monotonic() - t0
                timings.append(dt)
                if dt > 5.0:
                    stalls += 1
                if (i + 1) % 10 == 0:
                    print(f"  cycle {i+1}: dt={dt*1000:.0f}ms (running avg {statistics.mean(timings)*1000:.0f}ms, stalls={stalls})")
            except Exception as exc:
                errors += 1
                print(f"  cycle {i+1}: ERROR {type(exc).__name__}: {exc}")
            time.sleep(0.05)
    finally:
        try: plc.close()
        except Exception: pass
    if timings:
        print(f"  total {len(timings)} cycles | min {min(timings)*1000:.0f}ms | max {max(timings)*1000:.0f}ms | avg {statistics.mean(timings)*1000:.0f}ms | stalls(>5s) {stalls} | errors {errors}")


def bench_pylogix(ip: str, cycles: int):
    from pylogix import PLC
    print(f"\n=== pylogix (single read, all tags) ===")
    timings = []
    stalls = 0
    errors = 0
    comm = PLC()
    comm.IPAddress = ip
    comm.ProcessorSlot = 0
    try:
        # Warm-up read
        comm.Read(TAGS[0])
        for i in range(cycles):
            t0 = time.monotonic()
            try:
                resp = comm.Read(TAGS)  # pylogix accepts a list
                dt = time.monotonic() - t0
                timings.append(dt)
                if dt > 5.0:
                    stalls += 1
                # pylogix returns a list of Response objects
                if (i + 1) % 10 == 0:
                    n_ok = sum(1 for r in resp if str(getattr(r, "Status", "")).lower() == "success")
                    print(f"  cycle {i+1}: dt={dt*1000:.0f}ms ok={n_ok}/{len(TAGS)} (avg {statistics.mean(timings)*1000:.0f}ms, stalls={stalls})")
            except Exception as exc:
                errors += 1
                print(f"  cycle {i+1}: ERROR {type(exc).__name__}: {exc}")
            time.sleep(0.05)
    finally:
        try: comm.Close()
        except Exception: pass
    if timings:
        print(f"  total {len(timings)} cycles | min {min(timings)*1000:.0f}ms | max {max(timings)*1000:.0f}ms | avg {statistics.mean(timings)*1000:.0f}ms | stalls(>5s) {stalls} | errors {errors}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.10.240")
    ap.add_argument("--cycles", type=int, default=60)
    args = ap.parse_args()
    print(f"Benchmarking against {args.ip} | tags={len(TAGS)} | cycles={args.cycles}")
    # Run pycomm3 single-read, batched-8, then pylogix.
    bench_pycomm3(args.ip, args.cycles, batch_size=len(TAGS))   # one big read
    bench_pycomm3(args.ip, args.cycles, batch_size=8)            # batched
    bench_pylogix(args.ip, args.cycles)


if __name__ == "__main__":
    main()
