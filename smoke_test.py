#!/usr/bin/env python3
"""
Smoke test for a running HyperCopy instance.

Usage:
    python3 smoke_test.py [base_url]

Checks that the API is up, the bots are running, and the payloads have the
shape the dashboard expects. Exits non-zero on the first failure.
"""
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
failures = []


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main():
    print(f"Testing {BASE}\n")

    health = get("/api/health")
    check("health ok", health.get("ok") is True)
    check("bots started", health.get("bots", {}).get("started") is True)

    snap = get("/api/snapshot")
    check("snapshot has tiers", isinstance(snap.get("tiers"), dict) and bool(snap["tiers"]))
    check("snapshot has traders", isinstance(snap.get("traders"), dict) and bool(snap["traders"]))
    check("snapshot has signals", isinstance(snap.get("signals"), list))

    live = get("/api/live")
    check("live has wallets", isinstance(live.get("wallets"), int))
    check("live feed running", live.get("status", {}).get("running") is True)
    check("live has positions", isinstance(live.get("positions"), list))

    copy = get("/api/copy")
    check("copy mode set", copy.get("mode") in ("paper", "live"))
    check("copy has equity", isinstance(copy.get("equity"), (int, float)))
    check("copy has positions", isinstance(copy.get("positions"), dict))
    check("copy has equity curve", isinstance(copy.get("equityCurve"), list))

    wl = get("/api/watchlist")
    check("watchlist non-empty", len(wl.get("wallets", [])) > 0,
          f"{len(wl.get('wallets', []))} wallets")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
