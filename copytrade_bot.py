#!/usr/bin/env python3
"""
Copy-trade execution engine.

Consumes the live consensus feed and actually opens/closes positions.

Modes
  paper  (default) : simulated fills at the live mark price. Fully functional,
                     tracks equity, PnL, stops and targets. No keys, no funds.
  live             : real orders on Hyperliquid via the official SDK. Requires
                     HL_PRIVATE_KEY (agent/API wallet key) in the environment
                     and COPY_MODE=live. Refuses to run without them.

State -> data/copy_state.json
"""
import os, sys, time, json, threading, math
import hl

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
LIVE = os.path.join(DATA, "live.json")
STATE = os.path.join(DATA, "copy_state.json")
CONFIG = os.path.join(BASE, "config.json")

TICK_SECONDS = 10
_stop = threading.Event()
_status = {"running": False, "cycles": 0, "errors": 0, "lastError": None}
_sz_decimals = {}


# ---------------------------------------------------------------- config
def default_config():
    return {
        "mode": "paper",              # paper | live
        "startingEquity": 10000.0,
        "riskPerTradePct": 2.0,       # % of equity risked per entry
        "maxLeverage": 3.0,           # cap notional / equity
        "maxPositions": 5,
        "maxExposurePct": 300.0,      # total notional / equity cap
        "minHolders": 2,              # consensus breadth required
        "minAlignment": 0.66,
        "minConviction": 60.0,
        "stopLossPct": 6.0,
        "takeProfitPct": 14.0,
        "maxHoldMinutes": 720,
        "cooldownMinutes": 20,
        "allowCoins": [],             # empty = all
        "blockCoins": [],
        "slippage": 0.01,
    }


def load_config():
    cfg = hl.load_json(CONFIG, {}) or {}
    d = default_config()
    d.update(cfg)
    env_mode = os.environ.get("COPY_MODE")
    if env_mode in ("paper", "live"):
        d["mode"] = env_mode
    return d


# ---------------------------------------------------------------- helpers
def get_sz_decimals():
    global _sz_decimals
    if _sz_decimals:
        return _sz_decimals
    try:
        m = hl.meta()
        for a in m.get("universe", []):
            _sz_decimals[a["name"]] = int(a.get("szDecimals", 2))
    except Exception:
        pass
    return _sz_decimals


def round_sz(coin, sz):
    d = get_sz_decimals().get(coin, 2)
    f = 10 ** d
    return math.floor(sz * f) / f


def init_state(cfg):
    st = hl.load_json(STATE, None)
    if not st:
        st = {
            "mode": cfg["mode"],
            "equity": cfg["startingEquity"],
            "cash": cfg["startingEquity"],
            "realizedPnl": 0.0,
            "positions": {},
            "orders": [],
            "closed": [],
            "equityCurve": [{"ts": hl.now_ms(), "equity": cfg["startingEquity"]}],
            "cooldowns": {},
            "startedAt": hl.now_ms(),
        }
        hl.save_json(STATE, st)
    st["mode"] = cfg["mode"]
    return st


def log_order(st, o):
    st["orders"].insert(0, o)
    st["orders"] = st["orders"][:200]


# ---------------------------------------------------------------- exchange
class PaperBroker:
    mode = "paper"

    def __init__(self, cfg):
        self.cfg = cfg

    def open(self, coin, side, notional, px):
        return {"fillPx": px, "filled": notional / px if px else 0, "fee": notional * 0.00045}

    def close(self, coin, side, qty, px):
        return {"fillPx": px, "fee": abs(qty * px) * 0.00045}


class LiveBroker:
    mode = "live"

    def __init__(self, cfg):
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        key = os.environ.get("HL_PRIVATE_KEY")
        if not key:
            raise RuntimeError("HL_PRIVATE_KEY not set — refusing live mode")
        acct = Account.from_key(key)
        addr = os.environ.get("HL_ACCOUNT_ADDRESS") or acct.address
        self.exchange = Exchange(acct, base_url="https://api.hyperliquid.xyz",
                                 account_address=addr)
        self.cfg = cfg

    def open(self, coin, side, notional, px):
        sz = round_sz(coin, notional / px)
        if sz <= 0:
            raise RuntimeError("size rounds to zero")
        r = self.exchange.market_open(coin, side == "long", sz,
                                      slippage=self.cfg["slippage"])
        return {"fillPx": px, "filled": sz, "raw": r}

    def close(self, coin, side, qty, px):
        r = self.exchange.market_close(coin, sz=round_sz(coin, qty),
                                       slippage=self.cfg["slippage"])
        return {"fillPx": px, "raw": r}


def make_broker(cfg):
    if cfg["mode"] == "live":
        try:
            return LiveBroker(cfg)
        except Exception as e:
            print("[copy] live broker unavailable:", e, "-> falling back to paper", flush=True)
            cfg["mode"] = "paper"
    return PaperBroker(cfg)


# ---------------------------------------------------------------- engine
def conviction_weight(conv):
    return max(0.35, min(1.0, conv / 100.0))


def tick(st, broker, cfg, live):
    mids = hl.all_mids()
    px = {k: float(v) for k, v in mids.items()}
    signals = live.get("signals", [])
    sig_by_coin = {s["coin"]: s for s in signals}
    now = hl.now_ms()

    # ---- 1. manage open positions (exits first) ----
    for coin in list(st["positions"].keys()):
        p = st["positions"][coin]
        mark = px.get(coin, p["entryPx"])
        p["markPx"] = mark
        p["upnl"] = (mark - p["entryPx"]) * p["qty"] * (1 if p["side"] == "long" else -1)
        ret = (mark - p["entryPx"]) / p["entryPx"] * (1 if p["side"] == "long" else -1) if p["entryPx"] else 0
        p["ret"] = ret
        reason = None
        if ret <= -cfg["stopLossPct"] / 100.0:
            reason = "stop-loss"
        elif ret >= cfg["takeProfitPct"] / 100.0:
            reason = "take-profit"
        elif (now - p["openedAt"]) / 60000.0 >= cfg["maxHoldMinutes"]:
            reason = "max-hold"
        else:
            s = sig_by_coin.get(coin)
            if s and s["consensus"] and s["holders"] >= cfg["minHolders"]:
                flipped = (p["side"] == "long" and s["bias"] == "SHORT" and s["alignment"] >= cfg["minAlignment"]) or \
                          (p["side"] == "short" and s["bias"] == "LONG" and s["alignment"] >= cfg["minAlignment"])
                if flipped:
                    reason = "signal-flip"
        if reason:
            close_position(st, broker, cfg, coin, mark, reason)

    # ---- 2. entries ----
    open_count = len(st["positions"])
    exposure = sum(p["notional"] for p in st["positions"].values())
    for s in signals:
        if open_count >= cfg["maxPositions"]:
            break
        coin = s["coin"]
        if coin in st["positions"]:
            continue
        if cfg["allowCoins"] and coin not in cfg["allowCoins"]:
            continue
        if coin in cfg["blockCoins"]:
            continue
        if not s["consensus"] or s["holders"] < cfg["minHolders"]:
            continue
        if s["alignment"] < cfg["minAlignment"] or s["conviction"] < cfg["minConviction"]:
            continue
        cd = st["cooldowns"].get(coin, 0)
        if (now - cd) / 60000.0 < cfg["cooldownMinutes"]:
            continue
        mark = px.get(coin)
        if not mark:
            continue

        risk_notional = st["equity"] * (cfg["riskPerTradePct"] / 100.0) * conviction_weight(s["conviction"])
        max_notional = st["equity"] * cfg["maxLeverage"]
        room = max(0.0, st["equity"] * (cfg["maxExposurePct"] / 100.0) - exposure)
        notional = min(risk_notional, max_notional, room)
        if notional < 10:
            continue

        qty = notional / mark
        if cfg["mode"] == "live":
            qty = round_sz(coin, qty)
            if qty <= 0:
                continue
            notional = qty * mark

        try:
            fill = broker.open(coin, s["bias"].lower(), notional, mark)
        except Exception as e:
            _status["errors"] += 1
            _status["lastError"] = f"open {coin}: {e}"
            continue

        st["positions"][coin] = {
            "coin": coin, "side": s["bias"].lower(), "qty": fill.get("filled", qty),
            "entryPx": fill.get("fillPx", mark), "markPx": mark, "notional": notional,
            "openedAt": now, "conviction": s["conviction"], "holders": s["holders"],
            "alignment": s["alignment"], "upnl": 0.0, "ret": 0.0,
            "reason": f"{s['holders']} traders · {s['alignment']:.0%} aligned · conv {s['conviction']}",
        }
        st["cash"] -= fill.get("fee", 0)
        log_order(st, {"ts": now, "action": "OPEN", "coin": coin, "side": s["bias"],
                       "qty": round(st["positions"][coin]["qty"], 6), "px": mark,
                       "notional": round(notional, 2), "mode": cfg["mode"],
                       "why": st["positions"][coin]["reason"]})
        open_count += 1
        exposure += notional
        print(f"[copy] OPEN {coin} {s['bias']} ${notional:,.0f} @ {mark}", flush=True)

    # ---- 3. mark to market ----
    unreal = sum(p["upnl"] for p in st["positions"].values())
    st["equity"] = st["cash"] + unreal
    st["equityCurve"].append({"ts": now, "equity": round(st["equity"], 2)})
    st["equityCurve"] = st["equityCurve"][-600:]
    st["updatedAt"] = now
    st["unrealizedPnl"] = round(unreal, 2)
    st["openNotional"] = round(sum(p["notional"] for p in st["positions"].values()), 2)
    st["status"] = dict(_status)


def close_position(st, broker, cfg, coin, mark, reason):
    p = st["positions"][coin]
    try:
        fill = broker.close(coin, p["side"], p["qty"], mark)
    except Exception as e:
        _status["errors"] += 1
        _status["lastError"] = f"close {coin}: {e}"
        return
    pnl = (mark - p["entryPx"]) * p["qty"] * (1 if p["side"] == "long" else -1)
    fee = fill.get("fee", 0)
    st["cash"] += pnl - fee
    st["realizedPnl"] = st.get("realizedPnl", 0.0) + pnl - fee
    st["closed"].insert(0, {
        "coin": coin, "side": p["side"], "qty": round(p["qty"], 6),
        "entryPx": p["entryPx"], "exitPx": mark, "pnl": round(pnl - fee, 2),
        "ret": round(p["ret"] * 100, 2), "reason": reason,
        "openedAt": p["openedAt"], "closedAt": hl.now_ms(), "mode": cfg["mode"],
    })
    st["closed"] = st["closed"][:200]
    st["cooldowns"][coin] = hl.now_ms()
    log_order(st, {"ts": hl.now_ms(), "action": "CLOSE", "coin": coin,
                   "side": p["side"].upper(), "qty": round(p["qty"], 6), "px": mark,
                   "notional": round(p["qty"] * mark, 2), "mode": cfg["mode"],
                   "pnl": round(pnl - fee, 2), "why": reason})
    del st["positions"][coin]
    print(f"[copy] CLOSE {coin} {reason} pnl ${pnl - fee:,.2f}", flush=True)


def run():
    _status["running"] = True
    cfg = load_config()
    st = init_state(cfg)
    broker = make_broker(cfg)
    print(f"[copy] engine starting in {cfg['mode'].upper()} mode", flush=True)
    while not _stop.is_set():
        t0 = time.time()
        try:
            live = hl.load_json(LIVE, {}) or {}
            if live.get("signals"):
                tick(st, broker, cfg, live)
                _status["cycles"] += 1
                hl.save_json(STATE, st)
        except Exception as e:
            _status["errors"] += 1
            _status["lastError"] = str(e)
            print("[copy] error:", e, flush=True)
        _stop.wait(max(2, TICK_SECONDS - (time.time() - t0)))
    _status["running"] = False


def start():
    t = threading.Thread(target=run, daemon=True, name="copybot")
    t.start()
    return t


def stop():
    _stop.set()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        stop()
