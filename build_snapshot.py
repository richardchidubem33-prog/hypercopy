#!/usr/bin/env python3
"""
HyperCopy snapshot builder.

Pulls the live Hyperliquid leaderboard + per-wallet trade history/positions and
builds a copy-trading signal snapshot:

  1. Segment wallets into balance tiers.
  2. Rank best traders per tier across 30D / 90D / 1Y windows.
  3. Apply quality gates: win rate >= 70% and max trade size <= 5x account.
  4. Select the top 3 per tier (~15 wallets) to track.
  5. Aggregate their open positions into consensus copy-trade signals with
     conviction weighting (position size = validity).
"""
import json, os, time, math, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
CACHE = os.path.join(DATA, "cache")
os.makedirs(CACHE, exist_ok=True)

LB_FILE = os.path.join(BASE, "lb.json")
INFO_URL = "https://api.hyperliquid.xyz/info"
LB_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

TIERS = [
    ("0-5k", 0, 5000),
    ("5-10k", 5000, 10000),
    ("10-30k", 10000, 30000),
    ("30-50k", 30000, 50000),
    ("50-100k", 50000, 100000),
]
WINDOWS = ["30D", "90D", "1Y"]
WIN_MAP = {"30D": "month", "1Y": "allTime"}  # 90D computed from fills

MIN_ACCOUNT = {"0-5k": 500}  # avoid dust accounts distorting ROI
MIN_CLOSES = 10              # need enough trades for a meaningful win rate
WIN_RATE_MIN = 0.70
MAX_TRADE_MULT = 5.0
CANDIDATES_PER_TIER = 40     # enrich in ROI order until we have enough passers
TARGET_PASSERS = 6           # buffer of qualifying wallets per tier
TOP_PER_TIER = 3


def http_post(payload, timeout=25):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(INFO_URL, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get_window(row, key):
    for k, v in row["windowPerformances"]:
        if k == key:
            return v
    return None


def cache_path(addr, kind):
    return os.path.join(CACHE, f"{kind}_{addr.lower()}.json")


def fetch_cached(addr, kind, payload, max_age=6 * 3600):
    p = cache_path(addr, kind)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age:
        try:
            return json.load(open(p))
        except Exception:
            pass
    for attempt in range(4):
        try:
            d = http_post(payload)
            json.dump(d, open(p, "w"))
            return d
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        except Exception:
            time.sleep(0.8 * (attempt + 1))
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return None


def enrich(row):
    """Fetch positions + fills and compute quality metrics."""
    addr = row["ethAddress"]
    ch = fetch_cached(addr, "ch", {"type": "clearinghouseState", "user": addr})
    fills = fetch_cached(addr, "fills", {"type": "userFills", "user": addr})
    if ch is None:
        return None

    av = float(ch.get("marginSummary", {}).get("accountValue", 0) or 0)
    if av <= 0:
        av = float(row["accountValue"])
    fills = fills or []

    # ---- win rate from realized closes ----
    closes = [f for f in fills if abs(float(f.get("closedPnl", 0) or 0)) > 0]
    wins = sum(1 for f in closes if float(f["closedPnl"]) > 0)
    win_rate = (wins / len(closes)) if closes else 0.0

    # ---- 90D realized pnl + volume from fills ----
    now_ms = time.time() * 1000
    cutoff = now_ms - 90 * 86400 * 1000
    pnl90 = 0.0
    vlm90 = 0.0
    for f in fills:
        if f["time"] >= cutoff:
            pnl90 += float(f.get("closedPnl", 0) or 0)
            try:
                vlm90 += abs(float(f["px"]) * float(f["sz"]))
            except Exception:
                pass
    roi90 = (pnl90 / av) if av else 0.0

    # ---- positions + max leverage (max trade size / account) ----
    positions = []
    max_lev = 0.0
    for ap in ch.get("assetPositions", []):
        p = ap["position"]
        pv = abs(float(p.get("positionValue", 0) or 0))
        szi = float(p.get("szi", 0) or 0)
        lev = (pv / av) if av else 0.0
        max_lev = max(max_lev, lev)
        positions.append({
            "coin": p["coin"],
            "szi": szi,
            "side": "long" if szi > 0 else "short",
            "entryPx": float(p.get("entryPx", 0) or 0),
            "positionValue": pv,
            "unrealizedPnl": float(p.get("unrealizedPnl", 0) or 0),
            "leverage": (p.get("leverage", {}) or {}).get("value"),
        })

    # ---- historical max position notional from fills (informational) ----
    max_hist = 0.0
    for f in fills:
        try:
            px = float(f["px"]); sz = float(f["sz"])
            sp = float(f.get("startPosition", 0) or 0)
            signed = sz if f.get("side") == "B" else -sz
            post = sp + signed
            max_hist = max(max_hist, abs(post) * px)
        except Exception:
            continue
    # "trade size vs account" = live position notional / account value
    max_trade_ratio = max_lev

    passes = (win_rate >= WIN_RATE_MIN and max_trade_ratio <= MAX_TRADE_MULT
              and len(closes) >= MIN_CLOSES)

    return {
        "address": addr,
        "displayName": row.get("displayName"),
        "accountValue": av,
        "winRate": win_rate,
        "closes": len(closes),
        "wins": wins,
        "maxTradeRatio": max_trade_ratio,
        "currentMaxLev": max_lev,
        "roi90": roi90,
        "pnl90": pnl90,
        "vlm90": vlm90,
        "positions": positions,
        "passes": passes,
        "tier": None,
    }


def build_candidates(rows):
    """Per tier: ordered candidate list (best blended ROI first) for enrichment."""
    out = {}
    for name, lo, hi in TIERS:
        floor = MIN_ACCOUNT.get(name, lo)
        pool = []
        for r in rows:
            av = float(r["accountValue"])
            if not (max(lo, floor) <= av < hi):
                continue
            m = get_window(r, "month"); a = get_window(r, "allTime")
            if not m or not a:
                continue
            if float(m["vlm"]) < max(10000, av * 3):
                continue
            pool.append(r)
        # blended rank: average of month-ROI rank and allTime-ROI rank
        by_month = sorted(pool, key=lambda r: float(get_window(r, "month")["roi"]), reverse=True)
        by_all = sorted(pool, key=lambda r: float(get_window(r, "allTime")["roi"]), reverse=True)
        rank = {}
        for i, r in enumerate(by_month):
            rank[r["ethAddress"]] = rank.get(r["ethAddress"], 0) + i
        for i, r in enumerate(by_all):
            rank[r["ethAddress"]] = rank.get(r["ethAddress"], 0) + i
        ordered = sorted(pool, key=lambda r: rank.get(r["ethAddress"], 1e9))
        out[name] = ordered[:CANDIDATES_PER_TIER]
    return out


def window_roi(t, window):
    if window == "90D":
        return t["roi90"]
    w = get_window(t["_lb"], WIN_MAP[window])
    return float(w["roi"]) if w else 0.0


def window_pnl(t, window):
    if window == "90D":
        return t["pnl90"]
    w = get_window(t["_lb"], WIN_MAP[window])
    return float(w["pnl"]) if w else 0.0


def window_vlm(t, window):
    if window == "90D":
        return t["vlm90"]
    w = get_window(t["_lb"], WIN_MAP[window])
    return float(w["vlm"]) if w else 0.0


def enrich_tier(name, candidates):
    """Enrich in order, stopping once we have TARGET_PASSERS qualifying wallets."""
    results = []
    passers = 0
    idx = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        inflight = {}
        def submit_next():
            nonlocal idx
            if idx < len(candidates):
                r = candidates[idx]; idx += 1
                inflight[ex.submit(enrich, r)] = r
        for _ in range(5):
            submit_next()
        while inflight:
            fut = next(as_completed(list(inflight)))
            r = inflight.pop(fut)
            try:
                t = fut.result()
            except Exception:
                t = None
            if t:
                t["tier"] = name
                t["_lb"] = r
                results.append(t)
                if t["passes"]:
                    passers += 1
            if passers < TARGET_PASSERS and idx < len(candidates):
                submit_next()
            elif passers >= TARGET_PASSERS and not inflight:
                break
    return results, passers


def main():
    if not os.path.exists(LB_FILE) or time.time() - os.path.getmtime(LB_FILE) > 3600:
        print("Fetching leaderboard...")
        urllib.request.urlretrieve(LB_URL, LB_FILE)
    rows = json.load(open(LB_FILE))["leaderboardRows"]
    print(f"leaderboard rows: {len(rows)}")

    cands = build_candidates(rows)
    print("candidates:", {k: len(v) for k, v in cands.items()})

    traders = []
    for name, lst in cands.items():
        res, passers = enrich_tier(name, lst)
        traders.extend(res)
        print(f"  {name}: enriched {len(res)}, passers {passers}")

    print(f"enriched total: {len(traders)}  passing filters: {sum(1 for t in traders if t['passes'])}")

    # ---- per-window tier rankings + tracked selection ----
    out_tiers = {}
    tracked_all = {}
    for name, lo, hi in TIERS:
        tier_traders = [t for t in traders if t["tier"] == name]
        out_tiers[name] = {}
        for w in WINDOWS:
            def rank_key(t):
                # cap ROI so dust-account explosions don't dominate; tiebreak by PnL
                return (min(window_roi(t, w), 10.0), window_pnl(t, w))
            ranked = sorted(tier_traders, key=rank_key, reverse=True)
            table = []
            for t in ranked:
                table.append({
                    "address": t["address"],
                    "displayName": t["displayName"],
                    "accountValue": round(t["accountValue"], 2),
                    "roi": window_roi(t, w),
                    "pnl": window_pnl(t, w),
                    "volume": window_vlm(t, w),
                    "winRate": t["winRate"],
                    "closes": t["closes"],
                    "maxTradeRatio": t["maxTradeRatio"],
                    "passes": t["passes"],
                    "active": bool(t["positions"]),
                })
            # tracked = top 3 *active* traders in the tier that respect the 5x cap,
            # quality-first (70% qualified, then 60% signal-grade), then best ROI.
            eligible = [t for t in tier_traders
                        if t["maxTradeRatio"] <= MAX_TRADE_MULT
                        and (t["passes"] or t["winRate"] >= 0.60)]

            def qkey(t):
                return (1 if t["passes"] else 0,
                        1 if t["winRate"] >= 0.60 else 0,
                        t["winRate"], window_roi(t, w))

            active_sorted = sorted([t for t in eligible if t["positions"]],
                                   key=qkey, reverse=True)
            chosen = active_sorted[:TOP_PER_TIER]
            if len(chosen) < TOP_PER_TIER:
                have = {t["address"] for t in chosen}
                rest = sorted([t for t in eligible if t["address"] not in have],
                              key=qkey, reverse=True)
                chosen += rest[:TOP_PER_TIER - len(chosen)]
            tracked = []
            for t in chosen:
                tracked.append({
                    "address": t["address"],
                    "displayName": t["displayName"],
                    "accountValue": round(t["accountValue"], 2),
                    "roi": window_roi(t, w),
                    "pnl": window_pnl(t, w),
                    "winRate": t["winRate"],
                    "closes": t["closes"],
                    "maxTradeRatio": t["maxTradeRatio"],
                    "positions": t["positions"],
                    "active": bool(t["positions"]),
                    "qualified": t["passes"],
                    "signalGrade": t["winRate"] >= 0.60,
                })
                tracked_all.setdefault(t["address"], t)
            out_tiers[name][w] = {"table": table, "tracked": tracked}

    tracked_traders = list(tracked_all.values())
    # Consensus signals use a broader, still-quality "signal-grade" cohort:
    # active traders with >=60% win rate and <=5x leverage. (The copy list above
    # uses the strict >=70% qualifiers; signals need breadth to form consensus.)
    signal_cohort = [t for t in traders
                     if t["winRate"] >= 0.60
                     and t["maxTradeRatio"] <= MAX_TRADE_MULT
                     and t["positions"]]
    signals = build_signals(signal_cohort)

    # full detail map for every enriched trader (for the UI detail modal)
    trader_map = {}
    for t in traders:
        trader_map[t["address"]] = {
            "address": t["address"],
            "tier": t["tier"],
            "displayName": t["displayName"],
            "accountValue": round(t["accountValue"], 2),
            "winRate": t["winRate"],
            "closes": t["closes"],
            "wins": t["wins"],
            "maxTradeRatio": t["maxTradeRatio"],
            "roi30": window_roi(t, "30D"),
            "roi90": t["roi90"],
            "roi1y": window_roi(t, "1Y"),
            "pnl30": window_pnl(t, "30D"),
            "pnl90": t["pnl90"],
            "pnl1y": window_pnl(t, "1Y"),
            "passes": t["passes"],
            "active": bool(t["positions"]),
            "positions": t["positions"],
        }

    snapshot = {
        "generatedAt": int(time.time() * 1000),
        "meta": {
            "tiers": [{"name": n, "lo": lo, "hi": hi} for n, lo, hi in TIERS],
            "windows": WINDOWS,
            "winRateMin": WIN_RATE_MIN,
            "maxTradeMult": MAX_TRADE_MULT,
            "leaderboardSize": len(rows),
            "enriched": len(traders),
            "trackedCount": len(tracked_traders),
            "qualifiedCount": sum(1 for t in traders if t["passes"]),
            "signalGradeCount": sum(1 for t in traders if t["winRate"] >= 0.60
                                     and t["maxTradeRatio"] <= MAX_TRADE_MULT
                                     and t["positions"]),
            "signalCohort": len(signal_cohort),
            "activeTracked": sum(1 for t in tracked_traders if t["positions"]),
        },
        "tiers": out_tiers,
        "signals": signals,
        "traders": trader_map,
    }
    os.makedirs(DATA, exist_ok=True)
    json.dump(snapshot, open(os.path.join(DATA, "snapshot.json"), "w"), indent=1)
    print("signals:", len(signals))
    for s in signals[:8]:
        print(f"  {s['coin']:6s} {s['bias']:5s} align={s['alignment']:.0%} "
              f"long={s['longCount']} short={s['shortCount']} conv={s['conviction']:.0f}")
    print("wrote data/snapshot.json")


def build_signals(traders):
    """Aggregate open positions across tracked traders into consensus signals."""
    total_av = sum(t["accountValue"] for t in traders) or 1.0
    n_total = len(traders)
    n_active = sum(1 for t in traders if t["positions"])
    agg = {}
    for t in traders:
        for p in t["positions"]:
            c = p["coin"]
            a = agg.setdefault(c, {"long": [], "short": [], "longNtl": 0.0,
                                   "shortNtl": 0.0})
            rec = {"address": t["address"], "name": t["displayName"],
                   "notional": p["positionValue"], "szi": p["szi"],
                   "upnl": p["unrealizedPnl"], "lev": p["leverage"],
                   "accountValue": t["accountValue"], "winRate": t["winRate"],
                   "qualified": t["passes"]}
            if p["side"] == "long":
                a["long"].append(rec); a["longNtl"] += p["positionValue"]
            else:
                a["short"].append(rec); a["shortNtl"] += p["positionValue"]

    out = []
    for coin, a in agg.items():
        nl, ns = len(a["long"]), len(a["short"])
        holders = nl + ns
        if holders == 0:
            continue
        long_side = nl >= ns
        align = (max(nl, ns) / holders)
        bias = "LONG" if long_side else "SHORT"
        net = a["longNtl"] - a["shortNtl"]
        gross = a["longNtl"] + a["shortNtl"]
        cap_share = min(1.0, gross / (total_av * 0.5))
        size_conf = min(1.0, holders / 5.0)          # sample-size confidence
        conviction = round((align * 45 + size_conf * 35 + cap_share * 20), 1)
        dom_ntl = a["longNtl"] if long_side else a["shortNtl"]
        opp_ntl = a["shortNtl"] if long_side else a["longNtl"]
        out.append({
            "coin": coin,
            "bias": bias,
            "alignment": align,
            "longCount": nl,
            "shortCount": ns,
            "holders": holders,
            "activeTracked": n_active,
            "totalTracked": n_total,
            "longNotional": round(a["longNtl"], 2),
            "shortNotional": round(a["shortNtl"], 2),
            "netNotional": round(net, 2),
            "grossNotional": round(gross, 2),
            "conviction": conviction,
            "consensus": holders >= 2,
            "dominantNotional": round(dom_ntl, 2),
            "opposingNotional": round(opp_ntl, 2),
            "longs": sorted(a["long"], key=lambda x: -x["notional"]),
            "shorts": sorted(a["short"], key=lambda x: -x["notional"]),
        })
    # consensus (>=2 holders) first, then conviction
    out.sort(key=lambda s: (s["consensus"], s["conviction"], s["holders"]), reverse=True)
    return out


if __name__ == "__main__":
    main()
