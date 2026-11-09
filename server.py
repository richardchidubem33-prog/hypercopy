#!/usr/bin/env python3
"""HyperCopy web server: dashboard + snapshot API + live feed & copy bots."""
import os, sys, json, threading, subprocess
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import hl

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SNAP = os.path.join(DATA, "snapshot.json")
LIVE = os.path.join(DATA, "live.json")
COPY = os.path.join(DATA, "copy_state.json")
WATCH = os.path.join(DATA, "watchlist.json")
CONFIG = os.path.join(BASE, "config.json")
WEB = os.path.join(BASE, "web")

app = FastAPI(title="HyperCopy")
_build = {"active": False, "error": None}
_bots = {"started": False}


def run_build():
    _build["active"] = True
    _build["error"] = None
    try:
        subprocess.run([sys.executable, "build_snapshot.py"], cwd=BASE, timeout=900)
    except Exception as e:
        _build["error"] = str(e)
    finally:
        _build["active"] = False


def start_bots():
    if _bots["started"]:
        return
    import feed_bot, copytrade_bot
    feed_bot.start()
    copytrade_bot.start()
    _bots["started"] = True


@app.on_event("startup")
def _startup():
    start_bots()


# ------------------------------------------------------------------ data
@app.get("/api/snapshot")
def snapshot():
    if not os.path.exists(SNAP):
        return JSONResponse({"error": "no snapshot yet"}, status_code=503)
    return FileResponse(SNAP, media_type="application/json")


@app.get("/api/live")
def live():
    d = hl.load_json(LIVE, None)
    if d is None:
        return JSONResponse({"error": "feed warming up"}, status_code=503)
    return d


@app.get("/api/copy")
def copy_state():
    d = hl.load_json(COPY, None)
    if d is None:
        return JSONResponse({"error": "engine warming up"}, status_code=503)
    return d


@app.get("/api/events")
def events():
    return hl.load_json(os.path.join(DATA, "feed_events.json"), [])[:200]


@app.get("/api/health")
def health():
    return {"ok": True, "building": _build["active"], "error": _build["error"],
            "snapshot": os.path.exists(SNAP), "live": os.path.exists(LIVE),
            "copy": os.path.exists(COPY), "bots": _bots}


# ------------------------------------------------------------------ control
@app.post("/api/refresh")
def refresh():
    if _build["active"]:
        return {"status": "already_building"}
    threading.Thread(target=run_build, daemon=True).start()
    return {"status": "started"}


@app.get("/api/config")
def get_config():
    import copytrade_bot
    cfg = copytrade_bot.default_config()
    cfg.update(hl.load_json(CONFIG, {}) or {})
    return cfg


@app.post("/api/config")
async def set_config(req: Request):
    body = await req.json()
    import copytrade_bot
    cfg = copytrade_bot.default_config()
    cfg.update(hl.load_json(CONFIG, {}) or {})
    for k, v in body.items():
        if k in cfg:
            cfg[k] = v
    hl.save_json(CONFIG, cfg)
    return cfg


@app.get("/api/watchlist")
def get_watchlist():
    import feed_bot
    return feed_bot.load_watchlist()


@app.post("/api/watchlist")
async def set_watchlist(req: Request):
    body = await req.json()
    addr = (body.get("address") or "").lower()
    action = body.get("action", "toggle")
    wl = hl.load_json(WATCH, {"wallets": []})
    wallets = {w["address"].lower(): w for w in wl.get("wallets", [])}
    rec = {"address": addr, "tier": body.get("tier"), "name": body.get("name"),
           "winRate": body.get("winRate"), "qualified": body.get("qualified")}
    if action == "add":
        wallets[addr] = rec
    elif action == "remove":
        wallets.pop(addr, None)
    else:
        if addr in wallets:
            wallets.pop(addr)
        else:
            wallets[addr] = rec
    wl = {"wallets": list(wallets.values()), "updatedAt": hl.now_ms()}
    hl.save_json(WATCH, wl)
    return {"count": len(wl["wallets"]), "watching": addr in wallets}


@app.post("/api/copy/reset")
def reset_copy():
    if os.path.exists(COPY):
        os.remove(COPY)
    return {"status": "reset"}


@app.post("/api/bots/start")
def bots_start():
    start_bots()
    return {"status": "running", "bots": _bots}


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
