"""Shared Hyperliquid API helpers (public reads, no keys needed)."""
import json, time, urllib.request, urllib.error, os

INFO_URL = "https://api.hyperliquid.xyz/info"
LB_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
CACHE = os.path.join(DATA, "cache")
os.makedirs(CACHE, exist_ok=True)


def post(payload, timeout=20, retries=3):
    data = json.dumps(payload).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(INFO_URL, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(1.2 * (i + 1)); continue
            time.sleep(0.5 * (i + 1))
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def clearinghouse(addr):
    return post({"type": "clearinghouseState", "user": addr})


def all_mids():
    return post({"type": "allMids"})


def meta():
    return post({"type": "meta"})


def user_fills(addr):
    return post({"type": "userFills", "user": addr})


def positions_of(state):
    """Normalize clearinghouseState -> list of position dicts."""
    out = []
    av = float((state.get("marginSummary") or {}).get("accountValue", 0) or 0)
    for ap in state.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0) or 0)
        if szi == 0:
            continue
        pv = abs(float(p.get("positionValue", 0) or 0))
        out.append({
            "coin": p["coin"],
            "szi": szi,
            "side": "long" if szi > 0 else "short",
            "entryPx": float(p.get("entryPx", 0) or 0),
            "positionValue": pv,
            "unrealizedPnl": float(p.get("unrealizedPnl", 0) or 0),
            "leverage": (p.get("leverage") or {}).get("value"),
        })
    return out, av


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def now_ms():
    return int(time.time() * 1000)
