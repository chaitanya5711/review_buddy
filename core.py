"""ReviewBuddy core: shops, AI review drafting, AI insights, Google review count, storage.

No FastAPI imports here, so the logic can be tested on its own.
Storage: SQLite by default; Supabase (Postgres over REST) when SUPABASE_URL/KEY are set,
so data survives restarts on free hosting.
"""
import json
import os
import random
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

BASE = Path(__file__).parent
SHOPS_FILE = Path(os.getenv("SHOPS_FILE", BASE / "shops.json"))
DB_PATH = os.getenv("DB_PATH", str(BASE / "reviewbuddy.db"))

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PLACES_KEY = os.getenv("GOOGLE_PLACES_KEY", "")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

LANGS = {"en": "English", "hi": "Hindi", "mr": "Marathi"}
RATINGS = ["Excellent", "Good", "Okay", "Not good"]
EVENT_TYPES = {"scan", "ask", "remind", "generate", "copy_post", "google_count", "google_rating"}
FOLLOWUP_AFTER_HOURS = float(os.getenv("FOLLOWUP_AFTER_HOURS", "20"))
KEEP_PHONES_DAYS = 7  # phone numbers are hidden from staff after this

# ---------------------------------------------------------------- templates
TEMPLATES = [
    {"text": "We ordered {dish} and it was {taste}. The service was {service}.",
     "blanks": {"taste": ["delicious", "tasty", "okay", "not up to the mark"],
                "service": ["quick", "friendly", "slow", "average"]}},
    {"text": "I tried the {dish} here. Taste was {taste} and the place felt {ambience}.",
     "blanks": {"taste": ["amazing", "good", "average", "disappointing"],
                "ambience": ["cozy", "clean", "crowded", "noisy"]}},
    {"text": "Came here for {occasion}. The {dish} was {taste} and prices felt {price}.",
     "blanks": {"occasion": ["a family meal", "a quick bite", "lunch with friends", "dinner"],
                "taste": ["really good", "good", "okay", "not good"],
                "price": ["reasonable", "fair", "a bit high", "too high"]}},
    {"text": "The staff were {service} and the {dish} was {taste}. I would {again} visit again.",
     "blanks": {"service": ["very helpful", "polite", "a bit slow", "not attentive"],
                "taste": ["fresh and tasty", "good", "average", "not good"],
                "again": ["definitely", "probably", "maybe", "not"]}},
    {"text": "Best thing here was the {dish}. Overall experience was {overall}.",
     "blanks": {"overall": ["excellent", "good", "okay", "below expectations"]}},
    {"text": "Visited for {occasion}. Food was {taste}, waiting time was {wait}.",
     "blanks": {"occasion": ["breakfast", "lunch", "dinner", "snacks"],
                "taste": ["great", "good", "okay", "poor"],
                "wait": ["short", "reasonable", "long", "too long"]}},
]
GENERIC_TEMPLATES = [
    {"text": "I came for {dish} and the experience was {overall}. Staff were {service}.",
     "blanks": {"overall": ["excellent", "good", "okay", "not good"],
                "service": ["very helpful", "polite", "a bit slow", "not attentive"]}},
    {"text": "Got {dish} done here. Quality was {quality} and the place was {ambience}.",
     "blanks": {"quality": ["excellent", "good", "average", "poor"],
                "ambience": ["clean", "comfortable", "crowded", "not clean"]}},
    {"text": "Visited for {dish}. Waiting time was {wait} and pricing felt {price}.",
     "blanks": {"wait": ["short", "reasonable", "long", "too long"],
                "price": ["reasonable", "fair", "a bit high", "too high"]}},
    {"text": "The team explained things {explain}. Overall I am {overall} with {dish}.",
     "blanks": {"explain": ["very clearly", "clearly", "somewhat", "not clearly"],
                "overall": ["very happy", "happy", "okay", "not happy"]}},
]
LABELS = {
    "restaurant": {"overall": "Overall", "food": "Food", "service": "Service", "ambience": "Ambience", "item": "Dish"},
    "other": {"overall": "Overall", "food": "Quality", "service": "Staff", "ambience": "Place", "item": "Service / product"},
}


def is_food(shop):
    return shop.get("type", "restaurant") in ("restaurant", "cafe", "food")


def labels_for(shop):
    return LABELS["restaurant"] if is_food(shop) else LABELS["other"]


# ---------------------------------------------------------------- storage
class Store:
    """Tiny table store: SQLite locally, Supabase REST when configured."""
    SCHEMA = {
        "events": "id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, type TEXT, value REAL, ts REAL",
        "drafts": "id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, text TEXT, overall TEXT, lang TEXT, ai INTEGER, ts REAL",
        "asks": "id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT, phone TEXT, ts REAL, reminded_ts REAL, done INTEGER DEFAULT 0",
        "shops": "slug TEXT PRIMARY KEY, data TEXT, ts REAL",
    }

    def __init__(self):
        self.remote = bool(SUPABASE_URL and SUPABASE_KEY)
        self.lock = threading.Lock()
        if not self.remote:
            with self._con() as con:
                for t, cols in self.SCHEMA.items():
                    con.execute(f"CREATE TABLE IF NOT EXISTS {t}({cols})")

    def _con(self):
        return sqlite3.connect(DB_PATH)

    def _rest(self, method, path, body=None, prefer="return=representation"):
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json", "Prefer": prefer})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def insert(self, table, row):
        if self.remote:
            out = self._rest("POST", table, row)
            return out[0].get("id") if out else None
        with self.lock, self._con() as con:
            cols = ",".join(row)
            cur = con.execute(f"INSERT INTO {table}({cols}) VALUES({','.join('?' * len(row))})", list(row.values()))
            return cur.lastrowid

    def upsert_shop(self, slug, data):
        row = {"slug": slug, "data": json.dumps(data), "ts": time.time()}
        if self.remote:
            self._rest("POST", "shops", row, prefer="resolution=merge-duplicates,return=minimal")
            return
        with self.lock, self._con() as con:
            con.execute("INSERT OR REPLACE INTO shops(slug,data,ts) VALUES(?,?,?)", (slug, row["data"], row["ts"]))

    def select(self, table, slug=None, since=None):
        if self.remote:
            q = ["select=*", "order=ts.asc", "limit=50000"]
            if slug:
                q.append(f"slug=eq.{urllib.parse.quote(slug)}")
            if since:
                q.append(f"ts=gte.{since}")
            return self._rest("GET", f"{table}?{'&'.join(q)}") or []
        sql, args = f"SELECT * FROM {table} WHERE 1=1", []
        if slug:
            sql += " AND slug=?"; args.append(slug)
        if since:
            sql += " AND ts>=?"; args.append(since)
        with self._con() as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(sql + " ORDER BY ts", args).fetchall()]

    def update(self, table, row_id, fields):
        if self.remote:
            self._rest("PATCH", f"{table}?id=eq.{int(row_id)}", fields, prefer="return=minimal")
            return
        with self.lock, self._con() as con:
            sets = ",".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*fields.values(), int(row_id)])


store = Store()


# ---------------------------------------------------------------- shops
def place_id_of(shop):
    if shop.get("place_id"):
        return shop["place_id"]
    m = re.search(r"placeid=([\w-]+)", shop.get("review_url", ""))
    return m.group(1) if m else ""


def load_shops():
    shops = {}
    if SHOPS_FILE.exists():
        with open(SHOPS_FILE, encoding="utf-8") as f:
            shops = {s["slug"]: s for s in json.load(f)}
    for row in store.select("shops"):  # businesses added from the dashboard
        try:
            shops[row["slug"]] = json.loads(row["data"])
        except Exception:
            pass
    return shops


def clean_slug(s):
    return re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")[:40]


def save_shop(d):
    name = _clean(d.get("name"), 80)
    slug = clean_slug(d.get("slug") or name)
    review_url = (d.get("review_url") or "").strip()
    if not (name and slug and review_url.startswith("http")):
        raise ValueError("Name and a valid Google review link are required")
    menu = d.get("menu") or []
    if isinstance(menu, str):
        menu = [m.strip() for m in menu.split(",")]
    shop = {
        "slug": slug, "name": name, "type": d.get("type") or "restaurant",
        "menu": [_clean(m, 50) for m in menu if _clean(m, 50)][:20],
        "review_url": review_url, "staff_pin": str(d.get("staff_pin") or "1234")[:10],
        "baseline_reviews": int(d.get("baseline_reviews") or 0),
        "color": d.get("color") or "#e8590c", "emoji": (d.get("emoji") or "")[:4],
    }
    store.upsert_shop(slug, shop)
    return shop


def public_shop(shop):
    pool = TEMPLATES if is_food(shop) else GENERIC_TEMPLATES
    return {
        "slug": shop["slug"], "name": shop["name"], "type": shop.get("type", "restaurant"),
        "menu": shop.get("menu", []), "review_url": shop["review_url"],
        "color": shop.get("color", "#e8590c"), "emoji": shop.get("emoji", "☕" if is_food(shop) else "✨"),
        "templates": random.sample(pool, k=min(3, len(pool))),
        "labels": labels_for(shop), "ratings": RATINGS, "langs": LANGS,
        "today": len(store.select("drafts", slug=shop["slug"], since=today_start())),
    }


def today_start():
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def live_feed(shop):
    """Public, anonymous data for the counter display: counts + recent moods only (no names, no text)."""
    since = today_start()
    drafts = store.select("drafts", slug=shop["slug"], since=since)
    events = store.select("events", slug=shop["slug"], since=since)
    return {
        "name": shop["name"], "emoji": shop.get("emoji", ""), "color": shop.get("color", "#e8590c"),
        "today_feedback": len(drafts),
        "today_scans": sum(1 for e in events if e["type"] == "scan"),
        "today_google": sum(1 for e in events if e["type"] == "copy_post"),
        "recent": [{"mood": d["overall"] or "", "lang": d["lang"], "ts": d["ts"]} for d in drafts[-8:]][::-1],
    }


# ---------------------------------------------------------------- AI
def _clean(s, limit=600):
    return " ".join(str(s or "").split())[:limit]


def gemini(prompt, json_mode=False, temperature=0.9, timeout=20):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    cfg = {"temperature": temperature, "maxOutputTokens": 800}
    if json_mode:
        cfg["responseMimeType"] = "application/json"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    return out["candidates"][0]["content"]["parts"][0]["text"].strip()


def collect_inputs(data):
    ratings = data.get("ratings") or {}
    ratings = {k: v for k, v in ratings.items() if v in RATINGS and k in ("overall", "food", "service", "ambience")}
    items = [_clean(i, 60) for i in (data.get("items") or [])[:8] if _clean(i, 60)]
    return {
        "lang": data.get("lang") if data.get("lang") in LANGS else "en",
        "ratings": ratings, "items": items,
        "filled": _clean(data.get("filled")), "spoken": _clean(data.get("spoken")),
        "style": "detailed" if data.get("style") == "detailed" else "short",
        "variant": int(data.get("variant") or 0),
    }


def fallback_review(inp, words=LABELS["restaurant"]):
    """No-AI review built only from what the customer gave, in plain sentences."""
    parts = []
    r = {k: v.lower() for k, v in inp["ratings"].items()}
    if inp["items"]:
        verb = "had" if words is LABELS["restaurant"] else "came for"
        parts.append(f"I {verb} {' and '.join(inp['items'])}.")
    if inp["filled"]:
        parts.append(inp["filled"].rstrip(".") + ".")
    if "overall" in r:
        parts.append(f"Overall it was {r['overall']}.")
    aspects = [f"{words[k].lower()} was {r[k]}" for k in ("food", "service", "ambience") if k in r]
    if aspects:
        line = ", ".join(aspects[:-1]) + (" and " if len(aspects) > 1 else "") + aspects[-1]
        parts.append(line[0].upper() + line[1:] + ".")
    if inp["spoken"]:
        s = inp["spoken"]
        parts.append(s[0].upper() + s[1:].rstrip(".") + ".")
    return " ".join(parts).strip()


def build_prompt(inp, shop_name, words=LABELS["restaurant"]):
    lines = [f"- {words[k]}: {v}" for k, v in inp["ratings"].items()]
    length = "4 to 6 sentences" if inp["style"] == "detailed" else "2 to 3 short sentences"
    variant = (f"\n- This is attempt #{inp['variant'] + 1}: use clearly different wording and sentence order "
               f"from a typical version." if inp["variant"] else "")
    return f"""You help a real customer turn THEIR OWN feedback into a Google review for "{shop_name}".

STRICT RULES:
- Use ONLY what the customer said below. Never invent dishes, people, prices or details.
- Keep their sentiment exactly. Negative points stay in, said honestly and politely.
- Do not exaggerate ("good" must not become "amazing").
- {length}, first person, casual, like a real person typing on a phone. Vary the opening; do not start with "I recently".
- The customer may mix Hindi/Marathi/English (Hinglish). Understand it, and write in {LANGS[inp['lang']]}.
- No hashtags, no emojis, no star counts, no mention of AI, QR codes or being asked.{variant}
- Output only the review text.

Overall + quick ratings:
{chr(10).join(lines) or '- (none)'}
What they had / used: {', '.join(inp['items']) or '(not said)'}
Filled sentence: {inp['filled'] or '(none)'}
Their own words (typed or spoken): {inp['spoken'] or '(none)'}
"""


def make_review(data, shop):
    words = labels_for(shop)
    inp = collect_inputs(data)
    if not (inp["ratings"] or inp["filled"] or inp["spoken"] or inp["items"]):
        return {"review": "", "ai": False, "error": "empty"}
    text, ai = "", False
    if GEMINI_KEY:
        try:
            text = gemini(build_prompt(inp, shop["name"], words)).strip().strip('"')
            ai = bool(text)
        except Exception as e:  # network / quota / model errors -> fallback
            print("Gemini failed:", e)
    if not text:
        text = fallback_review(inp, words)
    store.insert("drafts", {"slug": shop["slug"], "text": text, "overall": inp["ratings"].get("overall", ""),
                            "lang": inp["lang"], "ai": int(ai), "ts": time.time()})
    log_event(shop["slug"], "generate")
    return {"review": text, "ai": ai}


STOP = set("""a an the and or but was were is are it its this that i we my our me to of in on for with at
very really so too bit just also they there here had have has be been place food service ambience
overall okay ok good not came visit visited time""".split())


def insights(shop, limit=200):
    """Owner-facing summary of what customers said (from drafted reviews)."""
    drafts = store.select("drafts", slug=shop["slug"])[-limit:]
    moods = Counter(d["overall"] or "Not given" for d in drafts)
    if not drafts:
        return {"summary": "No reviews drafted yet.", "loved": [], "issues": [], "actions": [], "moods": {}, "ai": False}
    if GEMINI_KEY:
        try:
            joined = "\n".join(f"- [{d['overall'] or '?'}] {d['text']}" for d in drafts)
            prompt = f"""You are a customer-experience analyst for a small Indian business "{shop['name']}".
Below are customer feedback drafts (with their overall mood). Reply as JSON with keys:
"summary" (1-2 sentences), "loved" (max 4 short phrases), "issues" (max 4 short phrases),
"actions" (3 concrete, cheap actions the owner can take this week). Be specific; use only what customers said.
Write in simple English.

{joined}"""
            out = json.loads(gemini(prompt, json_mode=True, temperature=0.3))
            out.update({"moods": dict(moods), "ai": True})
            return out
        except Exception as e:
            print("Insights AI failed:", e)
    words = Counter(w for d in drafts for w in re.findall(r"[a-zA-Z]{3,}", d["text"].lower()) if w not in STOP)
    happy = sum(moods.get(m, 0) for m in ("Excellent", "Good"))
    return {
        "summary": f"{len(drafts)} customers shared feedback; {happy} felt good or excellent.",
        "loved": [w for w, _ in words.most_common(6)][:4], "issues": [],
        "actions": ["Add a Gemini key to get AI insights on praise, complaints and next actions."],
        "moods": dict(moods), "ai": False,
    }


# ---------------------------------------------------------------- Google review count (optional)
def fetch_google_count(shop):
    """Uses Places API (New). Needs GOOGLE_PLACES_KEY. Returns (count, rating) or None."""
    pid = place_id_of(shop)
    if not (PLACES_KEY and pid):
        return None
    req = urllib.request.Request(f"https://places.googleapis.com/v1/places/{pid}",
                                 headers={"X-Goog-Api-Key": PLACES_KEY, "X-Goog-FieldMask": "userRatingCount,rating"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    return int(d.get("userRatingCount", 0)), float(d.get("rating", 0))


def refresh_google_counts():
    done = {}
    for slug, shop in load_shops().items():
        try:
            res = fetch_google_count(shop)
            if res:
                log_event(slug, "google_count", res[0])
                log_event(slug, "google_rating", res[1])
                done[slug] = {"count": res[0], "rating": res[1]}
        except Exception as e:
            print("Places API failed for", slug, e)
    return done


# ---------------------------------------------------------------- events, asks, follow-ups
def log_event(slug, etype, value=None):
    if etype not in EVENT_TYPES:
        raise ValueError("bad event type")
    store.insert("events", {"slug": slug, "type": etype, "value": value, "ts": time.time()})


def add_ask(slug, name, phone):
    log_event(slug, "ask")
    return store.insert("asks", {"slug": slug, "name": _clean(name, 40), "phone": re.sub(r"\D", "", phone or "")[:15],
                                 "ts": time.time(), "reminded_ts": None, "done": 0})


def followups(slug):
    """Customers asked 20h+ ago, not yet reminded, not marked as reviewed. One gentle reminder only."""
    now = time.time()
    rows = store.select("asks", slug=slug, since=now - KEEP_PHONES_DAYS * 86400)
    due = [r for r in rows if not r["done"] and not r["reminded_ts"] and now - r["ts"] >= FOLLOWUP_AFTER_HOURS * 3600]
    recent = [r for r in rows if now - r["ts"] < FOLLOWUP_AFTER_HOURS * 3600]
    pub = lambda r: {"id": r["id"], "name": r["name"], "phone": r["phone"], "ts": r["ts"]}
    return {"due": [pub(r) for r in due], "recent": [pub(r) for r in reversed(recent)][:20]}


def mark_ask(slug, ask_id, action):
    rows = [r for r in store.select("asks", slug=slug) if r["id"] == int(ask_id)]
    if not rows:
        raise ValueError("not found")
    if action == "reminded":
        store.update("asks", ask_id, {"reminded_ts": time.time()})
        log_event(slug, "remind")
    elif action == "done":
        store.update("asks", ask_id, {"done": 1})
    else:
        raise ValueError("bad action")


# ---------------------------------------------------------------- stats & report
def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def stats(shops):
    events = store.select("events")
    drafts = store.select("drafts")
    days = [_day(time.time() - i * 86400) for i in range(6, -1, -1)]
    out = []
    for slug, shop in shops.items():
        ev = [e for e in events if e["slug"] == slug]
        dr = [d for d in drafts if d["slug"] == slug]
        count = lambda t: sum(1 for e in ev if e["type"] == t)
        counts = [e for e in ev if e["type"] == "google_count" and e["value"] is not None]
        ratings = [e for e in ev if e["type"] == "google_rating" and e["value"] is not None]
        base = shop.get("baseline_reviews", 0)
        now = int(counts[-1]["value"]) if counts else base
        asked = count("ask") + count("scan")
        new_reviews = max(0, now - base)
        daily = {d: {"asked": 0, "drafted": 0, "posted": 0} for d in days}
        for e in ev:
            k = _day(e["ts"])
            if k in daily:
                if e["type"] in ("ask", "scan"):
                    daily[k]["asked"] += 1
                elif e["type"] == "generate":
                    daily[k]["drafted"] += 1
                elif e["type"] == "copy_post":
                    daily[k]["posted"] += 1
        out.append({
            "slug": slug, "name": shop["name"], "emoji": shop.get("emoji", ""),
            "asked_whatsapp": count("ask"), "reminders": count("remind"), "qr_scans": count("scan"),
            "reviews_drafted": count("generate"), "post_clicks": count("copy_post"),
            "baseline_reviews": base, "current_reviews": now, "new_reviews": new_reviews,
            "google_rating": ratings[-1]["value"] if ratings else None,
            "conversion": round(100 * new_reviews / asked, 1) if asked else 0,
            "moods": dict(Counter(d["overall"] or "Not given" for d in dr)),
            "langs": dict(Counter(LANGS.get(d["lang"], d["lang"]) for d in dr)),
            "ai_share": round(100 * sum(d["ai"] for d in dr) / len(dr)) if dr else 0,
            "daily": [{"day": d, **daily[d]} for d in days],
            "count_history": [{"value": e["value"], "ts": e["ts"]} for e in counts],
            "auto_count": bool(PLACES_KEY and place_id_of(shop)),
        })
    return out


def report_text(shops):
    s = stats(shops)
    asked = sum(x["asked_whatsapp"] + x["qr_scans"] for x in s)
    new = sum(x["new_reviews"] for x in s)
    lines = [f"ReviewBuddy results ({time.strftime('%d %b %Y, %I:%M %p')})", "",
             f"Businesses: {len(s)}", f"People asked (QR scans + WhatsApp): {asked}",
             f"Reviews drafted: {sum(x['reviews_drafted'] for x in s)}",
             f"Sent to Google: {sum(x['post_clicks'] for x in s)}",
             f"New Google reviews: {new}" + (f" ({round(100 * new / asked, 1)}% of people asked)" if asked else ""), ""]
    for x in s:
        lines.append(f"- {x['name']}: {x['baseline_reviews']} -> {x['current_reviews']} reviews (+{x['new_reviews']}); "
                     f"asked {x['asked_whatsapp'] + x['qr_scans']}, drafted {x['reviews_drafted']}, "
                     f"moods {x['moods'] or '-'}")
    return "\n".join(lines)


def export_csv(shops):
    rows = ["business,date,asked,drafted,sent_to_google"]
    for x in stats(shops):
        for d in x["daily"]:
            rows.append(f"\"{x['name']}\",{d['day']},{d['asked']},{d['drafted']},{d['posted']}")
    return "\n".join(rows) + "\n"
