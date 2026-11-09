#!/usr/bin/env python3
"""
Live feed bot.

Continuously polls every wallet on the watchlist, diffs their open positions and
emits real trade events (OPEN / ADD / REDUCE / CLOSE). Recomputes consensus
signals from live positions on every cycle.

Outputs:
  data/live.json          heartbeat, per-wallet positions, event ring buffer, live signals
  data/feed_events.json   append-only event log (capped)
"""
import os, sys, time, json, threading
import hl

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
WATCH = os.path.join(DATA, "watchlist.json")
LIVE = os.path.join(DATA, "live.json")
EVENTS = os.path.join(DATA, "feed_events.json")
CONFIG = os.path.join(BASE, "config.json")

POLL_SECONDS = int(os.environ.get("FEED_POLL_SECONDS", "15"))
MAX_EVENTS = 400
MAX_LOG = 5000

_stop = threading.Event()
_status = {"running": False, "cycles": 0, "errors": 0, "lastError": None}


def load_config():
    cfg = hl.load_json(CONFIG, {}) or {}
    return cfg


def load_watchlist():
    w = hl.load_json(WATCH, None)
    if not w:
        # seed from snapshot's tracked copy set
        snap = hl.load_json(os.path.join(DATA, "snapshot.json"), {}) or {}
        seen = {}
        for tier, wins in (snap.get("tiers") or {}).items():
            for wname, d in wins.items():
                for t in d.get("tracked", []):
                    seen.setdefault(t["address"], {"address": t["address"],
                                                   "tier": tier, "name": t.get("displayName")})
        w = {"wallets": list(seen.values()), "updatedAt": hl.now_ms()}
        hl.save_json(WATCH, w)
    return w


def diff_positions(prev, cur):
    """Compare two {coin: position} maps -> list of events."""
    ev = []
    coins = set(prev) | set(cur)
    for c in coins:
        p, n = prev.get(c), cur.get(c)
        if p is None and n is not None:
            ev.append({"kind": "OPEN", "coin": c, "side": n["side"],
                       "notional": n["positionValue"], "entryPx": n["entryPx"],
                       "szi": n["szi"]})
        elif p is not None and n is None:
            ev.append({"kind": "CLOSE", "coin": c, "side": p["side"],
                       "notional": p["positionValue"], "entryPx": p["entryPx"]})
        elif p is not None and n is not None:
            if p["side"] != n["side"]:
                ev.append({"kind": "FLIP", "coin": c, "side": n["side"],
                           "notional": n["positionValue"], "entryPx": n["entryPx"]})
            elif abs(n["positionValue"] - p["positionValue"]) > max(25, p["positionValue"] * 0.10):
                kind = "ADD" if n["positionValue"] > p["positionValue"] else "REDUCE"
                ev.append({"kind": kind, "coin": c, "side": n["side"],
                           "notional": n["positionValue"], "entryPx": n["entryPx"],
                           "delta": n["positionValue"] - p["positionValue"]})
    return ev


def compute_signals(wallet_states, mids):
    """Live consensus across watched wallets (same scoring as the snapshot)."""
    total_av = sum(w["accountValue"] for w in wallet_states) or 1.0
    n_active = sum(1 for w in wallet_states if w["positions"])
    agg = {}
    for w in wallet_states:
        for p in w["positions"]:
            a = agg.setdefault(p["coin"], {"long": [], "short": [], "ln": 0.0, "sn": 0.0})
            rec = {"address": w["address"], "name": w.get("name"),
                   "notional": p["positionValue"], "szi": p["szi"],
                   "upnl": p["unrealizedPnl"], "lev": p["leverage"],
                   "winRate": w.get("winRate"), "qualified": w.get("qualified")}
            if p["side"] == "long":
                a["long"].append(rec); a["ln"] += p["positionValue"]
            else:
                a["short"].append(rec); a["sn"] += p["positionValue"]
    out = []
    for coin, a in agg.items():
        nl, ns = len(a["long"]), len(a["short"])
        holders = nl + ns
        if not holders:
            continue
        long_side = nl >= ns
        align = max(nl, ns) / holders
        gross = a["ln"] + a["sn"]
        cap = min(1.0, gross / (total_av * 0.5))
        size_conf = min(1.0, holders / 5.0)
        out.append({
            "coin": coin,
            "bias": "LONG" if long_side else "SHORT",
            "alignment": align,
            "longCount": nl, "shortCount": ns, "holders": holders,
            "activeTracked": n_active, "totalTracked": len(wallet_states),
            "longNotional": round(a["ln"], 2), "shortNotional": round(a["sn"], 2),
            "grossNotional": round(gross, 2),
            "netNotional": round(a["ln"] - a["sn"], 2),
            "conviction": round(align * 45 + size_conf * 35 + cap * 20, 1),
            "consensus": holders >= 2,
            "markPx": float(mids.get(coin, 0) or 0),
            "longs": sorted(a["long"], key=lambda x: -x["notional"]),
            "shorts": sorted(a["short"], key=lambda x: -x["notional"]),
        })
    out.sort(key=lambda s: (s["consensus"], s["conviction"], s["holders"]), reverse=True)
    return out


def cycle(prev_positions, events):
    wl = load_watchlist()
    wallets = wl.get("wallets", [])
    mids = hl.all_mids()
    states = []
    new_events = []
    for w in wallets:
        addr = w["address"]
        try:
            ch = hl.clearinghouse(addr)
        except Exception as e:
            _status["errors"] += 1
            _status["lastError"] = f"{addr[:8]}: {e}"
            continue
        pos, av = hl.positions_of(ch)
        pmap = {p["coin"]: p for p in pos}
        prev = prev_positions.get(addr)
        if prev is None:
            # first observation = baseline, not a trade event
            prev_positions[addr] = pmap
        else:
            for e in diff_positions(prev, pmap):
                e.update({"address": addr, "name": w.get("name"), "tier": w.get("tier"),
                          "ts": hl.now_ms()})
                new_events.append(e)
            prev_positions[addr] = pmap
        states.append({"address": addr, "name": w.get("name"), "tier": w.get("tier"),
                       "accountValue": av, "positions": pos,
                       "winRate": w.get("winRate"), "qualified": w.get("qualified")})

    events = (new_events + events)[:MAX_EVENTS]
    signals = compute_signals(states, mids)

    live = {
        "ts": hl.now_ms(),
        "pollSeconds": POLL_SECONDS,
        "wallets": len(wallets),
        "activeWallets": sum(1 for s in states if s["positions"]),
        "equityTracked": round(sum(s["accountValue"] for s in states), 2),
        "signals": signals,
        "positions": [{"address": s["address"], "name": s["name"], "tier": s["tier"],
                       "accountValue": round(s["accountValue"], 2),
                       "positions": s["positions"]} for s in states],
        "events": events[:60],
        "status": dict(_status),
    }
    hl.save_json(LIVE, live)

    # append-only log
    log = hl.load_json(EVENTS, []) or []
    log = (new_events + log)[:MAX_LOG]
    hl.save_json(EVENTS, log)
    return new_events


def run():
    _status["running"] = True
    prev_positions = {}
    # Seed the ring buffer with recent persisted events so the dashboard has
    # history immediately after a restart (first poll still sets the baseline).
    events = hl.load_json(EVENTS, [])[:MAX_EVENTS]
    if events:
        print(f"[feed] seeded {len(events)} historical events", flush=True)
    print(f"[feed] starting, poll={POLL_SECONDS}s", flush=True)
    while not _stop.is_set():
        t0 = time.time()
        try:
            ev = cycle(prev_positions, events)
            events = ev + events
            _status["cycles"] += 1
            if ev:
                for e in ev[:5]:
                    print(f"[feed] {e['kind']:6s} {e['coin']:8s} {e['side']:5s} "
                          f"${e['notional']:,.0f} {e['address'][:10]}", flush=True)
        except Exception as e:
            _status["errors"] += 1
            _status["lastError"] = str(e)
            print("[feed] error:", e, flush=True)
        _stop.wait(max(2, POLL_SECONDS - (time.time() - t0)))
    _status["running"] = False


def start():
    t = threading.Thread(target=run, daemon=True, name="feedbot")
    t.start()
    return t


def stop():
    _stop.set()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        stop()
