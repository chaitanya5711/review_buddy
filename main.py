"""ReviewBuddy - helps every customer leave an honest Google review in under a minute.

Run locally:  uvicorn main:app --reload
"""
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

import core

app = FastAPI(title="ReviewBuddy")
STATIC = Path(__file__).parent / "static"
ADMIN_KEY = os.getenv("ADMIN_KEY", "admin123")


# ---------- helpers ----------
def get_shop(slug):
    shop = core.load_shops().get(slug or "")
    if not shop:
        raise HTTPException(404, "Shop not found")
    return shop


def check_admin(key):
    if key != ADMIN_KEY:
        raise HTTPException(401, "Wrong admin key")


def check_pin(shop, pin):
    if str(pin) != str(shop.get("staff_pin", "")):
        raise HTTPException(401, "Wrong PIN")


_hits = defaultdict(deque)


def rate_limit(req: Request, bucket: str, limit: int, per_seconds: int = 60):
    """Simple in-memory limiter so nobody can spam the AI endpoint."""
    ip = req.headers.get("x-forwarded-for", req.client.host if req.client else "?").split(",")[0].strip()
    q, now = _hits[(bucket, ip)], time.time()
    while q and now - q[0] > per_seconds:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests, please wait a minute")
    q.append(now)


# ---------- background: auto-refresh Google review counts (only if GOOGLE_PLACES_KEY is set) ----------
@app.on_event("startup")
def start_refresher():
    if not core.PLACES_KEY:
        return

    def loop():
        while True:
            core.refresh_google_counts()
            time.sleep(int(os.getenv("REFRESH_MINUTES", "30")) * 60)
    threading.Thread(target=loop, daemon=True).start()


# ---------- pages ----------
def page(name):
    return FileResponse(STATIC / name)


@app.get("/")
def home():
    return page("home.html")


@app.get("/r/{slug}")
def review_page(slug: str):
    get_shop(slug)
    return page("review.html")


@app.get("/staff/{slug}")
def staff_page(slug: str):
    get_shop(slug)
    return page("staff.html")


@app.get("/poster/{slug}")
def poster_page(slug: str):
    get_shop(slug)
    return page("poster.html")


@app.get("/live/{slug}")
def live_page(slug: str):
    get_shop(slug)
    return page("live.html")


@app.get("/dashboard")
def dashboard_page():
    return page("dashboard.html")


@app.get("/health")
def health():
    return {"ok": True, "ai": bool(core.GEMINI_KEY), "auto_count": bool(core.PLACES_KEY),
            "storage": "supabase" if core.store.remote else "sqlite"}


def lan_ip():
    """This computer's WiFi IP, so phones on the same WiFi can open the QR link."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no data is sent; just picks the WiFi interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@app.get("/api/base")
def api_base(request: Request):
    """Public link used inside QR codes and WhatsApp messages."""
    if os.getenv("PUBLIC_URL"):
        return {"base": os.getenv("PUBLIC_URL").rstrip("/"), "mode": "public"}
    host, port = request.url.hostname, request.url.port
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        return {"base": f"http://{lan_ip()}:{port or 8000}", "mode": "wifi"}
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return {"base": f"{proto}://{request.headers.get('host', host)}",
            "mode": "wifi" if host.startswith(("192.168.", "10.", "172.")) else "public"}


@app.get("/api/qr.svg")
def api_qr(data: str):
    """QR code generated inside the app (no outside service needed at the shop)."""
    if "/r/" not in data or len(data) > 300:
        raise HTTPException(400, "bad data")
    try:
        import io
        import segno
    except ImportError:
        raise HTTPException(501, "pip install segno")
    buf = io.BytesIO()
    segno.make_qr(data, error="m").save(buf, kind="svg", scale=10, border=0, dark="#1c1712")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ---------- customer API ----------
@app.get("/api/shops")
def api_shops():
    return [{"slug": s["slug"], "name": s["name"], "emoji": s.get("emoji", "")} for s in core.load_shops().values()]


@app.get("/api/shop/{slug}")
def api_shop(slug: str):
    return core.public_shop(get_shop(slug))


@app.get("/api/live/{slug}")
def api_live(slug: str):
    return core.live_feed(get_shop(slug))


@app.post("/api/event")
async def api_event(req: Request):
    rate_limit(req, "event", 60)
    d = await req.json()
    shop = get_shop(d.get("slug"))
    if d.get("type") not in {"scan", "copy_post"}:  # only these are public
        raise HTTPException(400, "bad event")
    core.log_event(shop["slug"], d["type"])
    return {"ok": True}


@app.post("/api/review")
async def api_review(req: Request):
    rate_limit(req, "review", 12)
    d = await req.json()
    return core.make_review(d, get_shop(d.get("slug")))


# ---------- staff API (PIN per shop) ----------
@app.post("/api/ask")
async def api_ask(req: Request):
    d = await req.json()
    shop = get_shop(d.get("slug"))
    check_pin(shop, d.get("pin"))
    ask_id = core.add_ask(shop["slug"], d.get("name"), d.get("phone"))
    return {"ok": True, "id": ask_id}


@app.post("/api/followups")
async def api_followups(req: Request):
    d = await req.json()
    shop = get_shop(d.get("slug"))
    check_pin(shop, d.get("pin"))
    return core.followups(shop["slug"])


@app.post("/api/followups/mark")
async def api_followup_mark(req: Request):
    d = await req.json()
    shop = get_shop(d.get("slug"))
    check_pin(shop, d.get("pin"))
    try:
        core.mark_ask(shop["slug"], d.get("id"), d.get("action"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------- admin API ----------
@app.get("/api/stats")
def api_stats(key: str):
    check_admin(key)
    return core.stats(core.load_shops())


@app.post("/api/google-count")
async def api_google_count(req: Request):
    d = await req.json()
    check_admin(d.get("key"))
    shop = get_shop(d.get("slug"))
    core.log_event(shop["slug"], "google_count", int(d["value"]))
    return {"ok": True}


@app.post("/api/google-count/refresh")
async def api_google_refresh(req: Request):
    d = await req.json()
    check_admin(d.get("key"))
    if not core.PLACES_KEY:
        raise HTTPException(400, "Set GOOGLE_PLACES_KEY to fetch counts automatically")
    return core.refresh_google_counts()


@app.post("/api/insights")
async def api_insights(req: Request):
    d = await req.json()
    check_admin(d.get("key"))
    return core.insights(get_shop(d.get("slug")))


@app.post("/api/shops")
async def api_add_shop(req: Request):
    d = await req.json()
    check_admin(d.get("key"))
    try:
        return core.save_shop(d)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/report")
def api_report(key: str):
    check_admin(key)
    return PlainTextResponse(core.report_text(core.load_shops()))


@app.get("/api/export.csv")
def api_export(key: str):
    check_admin(key)
    return PlainTextResponse(core.export_csv(core.load_shops()), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=reviewbuddy.csv"})
