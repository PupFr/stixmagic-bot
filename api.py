import os
import re
import sqlite3
import time
import uuid
import asyncio
import collections
import threading
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, g

DB_FILE = "bot.db"

app = Flask(__name__, static_folder="static")

API_KEY = os.environ.get("STIXMAGIC_API_KEY", "")
API_VERSION = "1.0"
PAGE_SIZE = 20

# Configurable CORS origin – defaults to "*" for development; set
# CORS_ALLOW_ORIGIN in production (e.g. "https://stixmagic.com").
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")


# ── Simple in-memory rate limiter ─────────────────────────────

class _RateLimiter:
    """Sliding-window rate limiter keyed by (IP, endpoint).

    Thread-safe; uses a deque per key to track request timestamps.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._windows: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def is_allowed(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            dq = self._windows[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

_limiter = _RateLimiter()


def rate_limit(limit: int = 60, window: float = 60.0):
    """Decorator: apply sliding-window rate limiting per client IP."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
            key = f"{ip}:{f.__name__}"
            if not _limiter.is_allowed(key, limit, window):
                return err("Too many requests — please slow down.", 429, "rate_limited")
            return f(*args, **kwargs)
        return wrapped
    return decorator


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def ok(data, status=200, **meta):
    body = {"ok": True, "data": data}
    body.update(meta)
    resp = jsonify(body)
    resp.status_code = status
    return resp


def err(message, status=400, code=None):
    body = {"ok": False, "error": {"message": message}}
    if code:
        body["error"]["code"] = code
    resp = jsonify(body)
    resp.status_code = status
    return resp


@app.before_request
def before_request():
    g.request_id = str(uuid.uuid4())[:8]
    g.start_time = time.monotonic()
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        return resp


@app.after_request
def add_headers(response):
    response.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
    response.headers["Access-Control-Allow-Headers"] = "X-API-Key, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    response.headers["X-API-Version"] = API_VERSION
    response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
    elapsed_ms = int((time.monotonic() - getattr(g, "start_time", time.monotonic())) * 1000)
    app.logger.info(
        "%s %s %s %dms req=%s",
        request.method,
        request.path,
        response.status_code,
        elapsed_ms,
        getattr(g, "request_id", "-"),
    )
    return response


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not API_KEY or key != API_KEY:
            return err("Valid API key required. Pass it as X-API-Key header or api_key param.", 401, "unauthorized")
        return f(*args, **kwargs)
    return decorated


def paginate(query_result):
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        limit = min(100, max(1, int(request.args.get("limit", PAGE_SIZE))))
    except ValueError:
        limit = PAGE_SIZE

    total = len(query_result)
    start = (page - 1) * limit
    items = query_result[start:start + limit]
    return items, {"page": page, "limit": limit, "total": total, "pages": max(1, -(-total // limit))}


# ── PUBLIC ────────────────────────────────────────────────────

@app.route("/")
def landing():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api")
@app.route("/api/")
def api_docs():
    return send_from_directory(app.static_folder, "api.html")


@app.route("/miniapp")
@app.route("/miniapp/")
def miniapp():
    return send_from_directory(app.static_folder, "miniapp.html")


# ── MINI APP (no API key — user_id comes from Telegram initData) ──

def _run_async(coro):
    """Run an async coroutine safely from a synchronous Flask route."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _validate_packs_async(token, user_id):
    """Validate all DB packs against Telegram; prune deleted, sync renamed titles."""
    from telegram import Bot as TelegramBot
    bot = TelegramBot(token=token)
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT name, title FROM packs WHERE user_id = ? ORDER BY id", (user_id,))
        rows = c.fetchall()
        conn.close()
        valid = []
        for row in rows:
            name, title = row["name"], row["title"]
            try:
                ss = await bot.get_sticker_set(name)
                if ss.title != title:
                    upd = get_db()
                    upd.execute(
                        "UPDATE packs SET title = ? WHERE user_id = ? AND name = ?",
                        (ss.title, user_id, name)
                    )
                    upd.commit()
                    upd.close()
                    title = ss.title
                valid.append({"name": name, "title": title, "link": f"https://t.me/addstickers/{name}"})
            except Exception:
                rm = get_db()
                rm.execute("DELETE FROM packs WHERE user_id = ? AND name = ?", (user_id, name))
                rm.commit()
                rm.close()
        return valid
    finally:
        await bot.close()


@app.route("/api/miniapp/packs")
@rate_limit(limit=30, window=60.0)
def miniapp_packs():
    user_id = request.args.get("user_id", "").strip()
    if not user_id or not user_id.isdigit():
        return err("Missing or invalid user_id", 400, "missing_param")
    uid = int(user_id)

    raw_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    token_match = re.search(r'\d+:[A-Za-z0-9_-]{35,}', raw_token)
    if token_match:
        try:
            packs = _run_async(_validate_packs_async(token_match.group(0), uid))
            return ok(packs)
        except Exception:
            pass

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name, title FROM packs WHERE user_id = ? ORDER BY id", (uid,))
    rows = c.fetchall()
    conn.close()
    return ok([
        {"name": r["name"], "title": r["title"],
         "link": f"https://t.me/addstickers/{r['name']}"}
        for r in rows
    ])


@app.route("/api/miniapp/settings")
@rate_limit(limit=30, window=60.0)
def miniapp_settings_get():
    user_id = request.args.get("user_id", "").strip()
    if not user_id or not user_id.isdigit():
        return err("Missing or invalid user_id", 400, "missing_param")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT mask_inverted FROM user_settings WHERE user_id = ?", (int(user_id),))
    row = c.fetchone()
    conn.close()
    return ok({"user_id": int(user_id), "mask_inverted": bool(row["mask_inverted"]) if row else False})


@app.route("/api/miniapp/settings", methods=["PATCH"])
@rate_limit(limit=20, window=60.0)
def miniapp_settings_patch():
    user_id = request.args.get("user_id", "").strip()
    if not user_id or not user_id.isdigit():
        return err("Missing or invalid user_id", 400, "missing_param")
    data = request.get_json(silent=True)
    if not data:
        return err("JSON body required", 400, "invalid_body")
    conn = get_db()
    c = conn.cursor()
    if "mask_inverted" in data:
        val = int(bool(data["mask_inverted"]))
        c.execute(
            "INSERT INTO user_settings (user_id, mask_inverted) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET mask_inverted = ?",
            (int(user_id), val, val)
        )
    conn.commit()
    c.execute("SELECT mask_inverted FROM user_settings WHERE user_id = ?", (int(user_id),))
    row = c.fetchone()
    conn.close()
    return ok({"user_id": int(user_id), "mask_inverted": bool(row["mask_inverted"]) if row else False})


@app.route("/api/health")
@rate_limit(limit=60, window=60.0)
def health():
    conn = get_db()
    try:
        conn.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    finally:
        conn.close()
    return ok({
        "status": "ok",
        "service": "stixmagic",
        "version": API_VERSION,
        "db": "ok" if db_ok else "error",
        "timestamp": int(time.time()),
    })


# ── AUTHENTICATED ─────────────────────────────────────────────

@app.route("/api/stats")
@require_api_key
@rate_limit(limit=30, window=60.0)
def stats():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(DISTINCT user_id) FROM packs")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM packs")
    total_packs = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT user_id) FROM user_settings")
    total_settings_users = c.fetchone()[0]
    conn.close()
    return ok({
        "users": total_users,
        "packs": total_packs,
        "users_with_settings": total_settings_users,
    })


@app.route("/api/search")
@require_api_key
@rate_limit(limit=30, window=60.0)
def search_packs():
    q = request.args.get("q", "").strip()
    if not q:
        return err("Missing required query param 'q'", 400, "missing_param")
    if len(q) < 2:
        return err("Query must be at least 2 characters", 400, "query_too_short")

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, name, title FROM packs WHERE title LIKE ? OR name LIKE ? ORDER BY title",
        (f"%{q}%", f"%{q}%")
    )
    rows = c.fetchall()
    conn.close()

    all_results = [
        {"user_id": r["user_id"], "name": r["name"], "title": r["title"],
         "link": f"https://t.me/addstickers/{r['name']}"}
        for r in rows
    ]
    items, pagination = paginate(all_results)
    return ok(items, query=q, pagination=pagination)


@app.route("/api/packs/<int:user_id>")
@require_api_key
def user_packs(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name, title FROM packs WHERE user_id = ? ORDER BY id", (user_id,))
    rows = c.fetchall()
    conn.close()

    all_packs = [
        {"name": r["name"], "title": r["title"], "link": f"https://t.me/addstickers/{r['name']}"}
        for r in rows
    ]
    items, pagination = paginate(all_packs)
    return ok(items, user_id=user_id, pagination=pagination)


@app.route("/api/packs/<int:user_id>/<pack_name>")
@require_api_key
def pack_detail(user_id, pack_name):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name, title FROM packs WHERE user_id = ? AND name = ?", (user_id, pack_name))
    row = c.fetchone()
    conn.close()
    if not row:
        return err("Pack not found", 404, "not_found")
    return ok({
        "user_id": user_id,
        "name": row["name"],
        "title": row["title"],
        "link": f"https://t.me/addstickers/{row['name']}",
    })


@app.route("/api/packs/<int:user_id>/<pack_name>", methods=["DELETE"])
@require_api_key
def delete_pack(user_id, pack_name):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM packs WHERE user_id = ? AND name = ?", (user_id, pack_name))
    row = c.fetchone()
    if not row:
        conn.close()
        return err("Pack not found", 404, "not_found")
    c.execute("DELETE FROM packs WHERE user_id = ? AND name = ?", (user_id, pack_name))
    conn.commit()
    conn.close()
    return ok({"deleted": True, "name": pack_name})


@app.route("/api/settings/<int:user_id>")
@require_api_key
def user_settings_get(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT mask_inverted FROM user_settings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return ok({
        "user_id": user_id,
        "mask_inverted": bool(row["mask_inverted"]) if row else False,
    })


@app.route("/api/settings/<int:user_id>", methods=["PATCH"])
@require_api_key
def user_settings_update(user_id):
    data = request.get_json(silent=True)
    if not data:
        return err("JSON body required", 400, "invalid_body")

    conn = get_db()
    c = conn.cursor()

    if "mask_inverted" in data:
        val = int(bool(data["mask_inverted"]))
        c.execute(
            "INSERT INTO user_settings (user_id, mask_inverted) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET mask_inverted = ?",
            (user_id, val, val)
        )

    conn.commit()
    c.execute("SELECT mask_inverted FROM user_settings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return ok({
        "user_id": user_id,
        "mask_inverted": bool(row["mask_inverted"]) if row else False,
    })


@app.route("/api/events")
@require_api_key
@rate_limit(limit=20, window=60.0)
def event_stats():
    """Return top event counts from the event_log for observability."""
    try:
        from infra.db import get_event_counts
        events = get_event_counts(limit=20)
    except Exception as exc:
        app.logger.error("event_stats error: %s", exc)
        return err("Could not read event log", 500, "server_error")
    return ok(events)


# ── ERRORS ────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api"):
        return err("Endpoint not found", 404, "not_found")
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(405)
def method_not_allowed(e):
    return err("Method not allowed", 405, "method_not_allowed")


@app.errorhandler(500)
def server_error(e):
    return err("Internal server error", 500, "server_error")


def run_api():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
