import os
import re
import html
import sqlite3
import logging
import io
import string
import random
import threading
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageOps
from telegram import Update, InputSticker, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ConversationHandler, ContextTypes, CallbackQueryHandler
)
from menus import build_keyboard, get_menu_text

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # ── Existing tables ──────────────────────────────────────────
    c.execute('''
        CREATE TABLE IF NOT EXISTS packs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            title TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            mask_inverted INTEGER DEFAULT 0
        )
    ''')

    # ── User registry ─────────────────────────────────────────────
    # Tracks every Telegram user who interacts with the bot along with
    # their subscription plan (free / premium / pro).
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username   TEXT,
            plan       TEXT NOT NULL DEFAULT 'free',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')

    # ── Creation-limit tracking ───────────────────────────────────
    # Records how many stickers a user has generated within a billing
    # period (daily for free plan, monthly for premium/pro).
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_usage (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES users(id),
            period_type    TEXT NOT NULL,
            period_start   TEXT NOT NULL,
            period_end     TEXT NOT NULL,
            creations_used INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')

    # ── Draft vault ───────────────────────────────────────────────
    # Every generated sticker lives here first.  It must be explicitly
    # approved before it can be published to a pack or collection.
    # Pipeline: generated → draft → approved → published
    #           generated → draft → rejected  → trash
    c.execute('''
        CREATE TABLE IF NOT EXISTS sticker_drafts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL REFERENCES users(id),
            source_file_id    TEXT,
            generated_file_id TEXT,
            status            TEXT NOT NULL DEFAULT 'draft',
            style_id          INTEGER REFERENCES catalog_styles(id),
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at        TEXT
        )
    ''')

    # ── Sticker collections ───────────────────────────────────────
    # Named groups that hold approved stickers (analogous to albums).
    c.execute('''
        CREATE TABLE IF NOT EXISTS sticker_collections (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            name       TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')

    # ── Collection items ──────────────────────────────────────────
    # Links an approved draft to a collection and stores the Telegram
    # file_id for fast retrieval.
    c.execute('''
        CREATE TABLE IF NOT EXISTS collection_items (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id    INTEGER NOT NULL REFERENCES sticker_collections(id),
            draft_id         INTEGER NOT NULL REFERENCES sticker_drafts(id),
            telegram_file_id TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')

    # ── Style catalog ─────────────────────────────────────────────
    # Defines the visual styles available during sticker creation.
    # plan_access controls which plan tier can access each style.
    c.execute('''
        CREATE TABLE IF NOT EXISTS catalog_styles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            slug          TEXT NOT NULL UNIQUE,
            description   TEXT,
            preview_image TEXT,
            category      TEXT,
            plan_access   TEXT NOT NULL DEFAULT 'free',
            status        TEXT NOT NULL DEFAULT 'active',
            instructions  TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')

    conn.commit()
    conn.close()

init_db()


def get_mask_inverted(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT mask_inverted FROM user_settings WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row[0]) if row else False

def set_mask_inverted(user_id, inverted):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'INSERT INTO user_settings (user_id, mask_inverted) VALUES (?, ?) '
        'ON CONFLICT(user_id) DO UPDATE SET mask_inverted = ?',
        (user_id, int(inverted), int(inverted))
    )
    conn.commit()
    conn.close()

def add_pack_to_db(user_id, name, title):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO packs (user_id, name, title) VALUES (?, ?, ?)', (user_id, name, title))
    conn.commit()
    conn.close()

def delete_pack_from_db(user_id, name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM packs WHERE user_id = ? AND name = ?', (user_id, name))
    conn.commit()
    conn.close()

def get_user_packs(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT name, title FROM packs WHERE user_id = ?', (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def update_pack_title_in_db(user_id, name, title):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE packs SET title = ? WHERE user_id = ? AND name = ?', (title, user_id, name))
    conn.commit()
    conn.close()

async def validate_and_sync_packs(bot, user_id):
    """Check each DB pack against Telegram. Prune deleted packs, sync renamed titles."""
    packs = get_user_packs(user_id)
    valid = []
    for name, title in packs:
        try:
            ss = await bot.get_sticker_set(name)
            if ss.title != title:
                update_pack_title_in_db(user_id, name, ss.title)
            valid.append((name, ss.title))
        except Exception:
            delete_pack_from_db(user_id, name)
            logger.info(f"Pruned stale pack {name} for user {user_id}")
    return valid

def is_new_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM packs WHERE user_id = ?', (user_id,))
    packs = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM user_settings WHERE user_id = ?', (user_id,))
    settings = c.fetchone()[0]
    conn.close()
    return packs == 0 and settings == 0


# ── Plan limits ───────────────────────────────────────────────
# Defines creation quotas and draft caps per subscription tier.
PLAN_LIMITS = {
    'free':    {'period': 'daily',   'creations': 3,   'max_drafts': 10},
    'premium': {'period': 'monthly', 'creations': 50,  'max_drafts': 100},
    'pro':     {'period': 'monthly', 'creations': 300, 'max_drafts': None},
}

# Drafts older than this many days are automatically expired by the cleanup worker.
# Configurable here; future versions may vary this per plan.
DRAFT_EXPIRY_DAYS = 7


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-naive datetime.

    Uses datetime.now(timezone.utc) (Python 3.12-compatible) and strips
    the tzinfo so it stays compatible with SQLite's datetime() strings.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── User-registry helpers ─────────────────────────────────────

def get_or_create_user(telegram_id, username=None):
    """Return the internal users row, creating it on first contact."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, plan FROM users WHERE telegram_id = ?', (telegram_id,))
    row = c.fetchone()
    if row:
        if username:
            c.execute(
                "UPDATE users SET username = ?, updated_at = datetime('now') WHERE telegram_id = ?",
                (username, telegram_id)
            )
            conn.commit()
        conn.close()
        return {'id': row[0], 'plan': row[1]}
    c.execute(
        "INSERT INTO users (telegram_id, username) VALUES (?, ?)",
        (telegram_id, username)
    )
    conn.commit()
    user_id = c.lastrowid
    conn.close()
    return {'id': user_id, 'plan': 'free'}


def get_user_plan(telegram_id):
    """Return the plan string for a Telegram user ('free', 'premium', 'pro')."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT plan FROM users WHERE telegram_id = ?', (telegram_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 'free'


# ── Usage-tracking helpers ────────────────────────────────────

def _current_period_bounds(period_type):
    """Return (period_start, period_end) ISO strings for the current billing window."""
    now = _utcnow()
    if period_type == 'daily':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    else:  # monthly
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


def check_creation_limit(telegram_id):
    """Return (allowed: bool, remaining: int) for the user's current period."""
    plan = get_user_plan(telegram_id)
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])
    period_type = limits['period']
    max_creations = limits['creations']
    period_start, period_end = _current_period_bounds(period_type)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT creations_used FROM user_usage "
        "WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?) "
        "AND period_type = ? AND period_start = ?",
        (telegram_id, period_type, period_start)
    )
    row = c.fetchone()
    conn.close()
    used = row[0] if row else 0
    remaining = max(0, max_creations - used)
    return remaining > 0, remaining


def increment_usage(telegram_id):
    """Increment the creation counter for the user's current billing period."""
    plan = get_user_plan(telegram_id)
    period_type = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])['period']
    period_start, period_end = _current_period_bounds(period_type)

    user = get_or_create_user(telegram_id)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT id FROM user_usage WHERE user_id = ? AND period_type = ? AND period_start = ?",
        (user['id'], period_type, period_start)
    )
    row = c.fetchone()
    if row:
        c.execute(
            "UPDATE user_usage SET creations_used = creations_used + 1 WHERE id = ?",
            (row[0],)
        )
    else:
        c.execute(
            "INSERT INTO user_usage (user_id, period_type, period_start, period_end, creations_used) "
            "VALUES (?, ?, ?, ?, 1)",
            (user['id'], period_type, period_start, period_end)
        )
    conn.commit()
    conn.close()


# ── Draft-vault helpers ───────────────────────────────────────

def create_draft(telegram_id, source_file_id=None, generated_file_id=None, style_id=None):
    """Insert a new draft and return its row id."""
    user = get_or_create_user(telegram_id)
    expires_at = (_utcnow() + timedelta(days=DRAFT_EXPIRY_DAYS)).isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO sticker_drafts "
        "(user_id, source_file_id, generated_file_id, style_id, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user['id'], source_file_id, generated_file_id, style_id, expires_at)
    )
    conn.commit()
    draft_id = c.lastrowid
    conn.close()
    return draft_id


def get_user_drafts(telegram_id, status=None):
    """Return draft rows for a user, optionally filtered by status."""
    user = get_or_create_user(telegram_id)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if status:
        c.execute(
            "SELECT id, generated_file_id, status, created_at, expires_at "
            "FROM sticker_drafts WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
            (user['id'], status)
        )
    else:
        c.execute(
            "SELECT id, generated_file_id, status, created_at, expires_at "
            "FROM sticker_drafts WHERE user_id = ? ORDER BY created_at DESC",
            (user['id'],)
        )
    rows = c.fetchall()
    conn.close()
    return rows


def update_draft_status(draft_id, status):
    """Change the lifecycle status of a draft."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE sticker_drafts SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, draft_id)
    )
    conn.commit()
    conn.close()


# ── Cleanup worker ────────────────────────────────────────────

def cleanup_worker():
    """Background thread: expire drafts that have passed their expiry date.

    Runs once per day. Moves stale 'draft' rows to 'expired' so the
    draft vault stays clean without manual intervention.
    """
    while True:
        try:
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                c.execute(
                    "UPDATE sticker_drafts "
                    "SET status = 'expired', updated_at = datetime('now') "
                    "WHERE status = 'draft' AND expires_at < datetime('now')"
                )
                expired_count = c.rowcount
                conn.commit()
            if expired_count:
                logger.info(f"Cleanup: expired {expired_count} draft(s)")
        except Exception as e:
            logger.error(f"Cleanup worker error: {e}")
        time.sleep(86400)  # run once per day


WAITING_TITLE, WAITING_STICKER = range(2)
CHOOSING_PACK, WAITING_STICKER_ADD = range(2, 4)
WAITING_SOURCE_IMAGE, WAITING_MASK_IMAGE, WAITING_CUT_PACK = range(4, 7)
WAITING_SYNC_NAME = 7

STICKER_EMOJI = ["✨"]

DIV = "◈ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ◈"

def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✕ Cancel", callback_data="nav:home")]])

def home_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✦ Home", callback_data="nav:home")]])

def back_home_keyboard(back):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◂ Back", callback_data=f"nav:{back}"),
        InlineKeyboardButton("✦ Home", callback_data="nav:home"),
    ]])


def extract_file_info(message):
    if message.sticker:
        fmt = "video" if message.sticker.is_video else "static"
        return message.sticker.file_id, "sticker", fmt
    elif message.photo:
        return message.photo[-1].file_id, "image", "static"
    elif message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("image/"):
            return message.document.file_id, "image", "static"
        elif mime.startswith("video/") or mime == "image/gif":
            return message.document.file_id, "video", "video"
        return message.document.file_id, "image", "static"
    elif message.video:
        return message.video.file_id, "video", "video"
    elif message.animation:
        return message.animation.file_id, "video", "video"
    elif message.video_note:
        return message.video_note.file_id, "video", "video"
    return None, None, None

async def download_file_bytes(bot, file_id):
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return None


def convert_video_to_sticker(file_bytes):
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_in:
            tmp_in.write(file_bytes.getvalue())
            tmp_in_path = tmp_in.name

        tmp_out_path = tmp_in_path.replace(".mp4", "_out.webm")

        def run_ffmpeg(bitrate, out_path):
            cmd = [
                "ffmpeg", "-y", "-i", tmp_in_path,
                "-vf", "scale='if(gt(iw,ih),512,-2)':'if(gt(iw,ih),-2,512)',fps=30",
                "-c:v", "libvpx-vp9",
                "-b:v", bitrate,
                "-t", "3",
                "-an",
                "-pix_fmt", "yuva420p",
                out_path
            ]
            return subprocess.run(cmd, capture_output=True, timeout=30)

        result = run_ffmpeg("200k", tmp_out_path)

        if result.returncode != 0:
            logger.error(f"ffmpeg error: {result.stderr.decode()[:500]}")
            return None

        with open(tmp_out_path, "rb") as f:
            data = f.read()

        if len(data) > 256000:
            os.unlink(tmp_out_path)
            tmp_out_path2 = tmp_in_path.replace(".mp4", "_out2.webm")
            run_ffmpeg("100k", tmp_out_path2)
            if os.path.exists(tmp_out_path2):
                with open(tmp_out_path2, "rb") as f:
                    data = f.read()
                os.unlink(tmp_out_path2)
            tmp_out_path = tmp_out_path2

        os.unlink(tmp_in_path)
        if os.path.exists(tmp_out_path):
            os.unlink(tmp_out_path)

        return io.BytesIO(data)
    except Exception as e:
        logger.error(f"Video conversion error: {e}")
        return None


def convert_to_sticker(file_bytes):
    try:
        img = Image.open(file_bytes)
    except Exception:
        return None

    if img.mode != "RGBA":
        img = img.convert("RGBA")

    max_dim = 512
    w, h = img.size
    if w > h:
        new_w = max_dim
        new_h = int(h * max_dim / w)
    else:
        new_h = max_dim
        new_w = int(w * max_dim / h)

    if (new_w, new_h) != (w, h):
        img = img.resize((new_w, new_h), Image.LANCZOS)

    output = io.BytesIO()
    img.save(output, format="WEBP", quality=80)
    if output.tell() > 64000:
        for q in [60, 40, 20]:
            output = io.BytesIO()
            img.save(output, format="WEBP", quality=q)
            if output.tell() <= 64000:
                break
    output.seek(0)
    return output


def apply_mask_to_image(source_bytes, mask_bytes, inverted=False):
    source = Image.open(source_bytes).convert("RGBA")
    mask = Image.open(mask_bytes).convert("L")
    mask = mask.resize(source.size, Image.LANCZOS)

    if inverted:
        mask = ImageOps.invert(mask)

    result = source.copy()
    result.putalpha(mask)

    max_dim = 512
    w, h = result.size
    if w > h:
        new_w = max_dim
        new_h = int(h * max_dim / w)
    else:
        new_h = max_dim
        new_w = int(w * max_dim / h)
    result = result.resize((new_w, new_h), Image.LANCZOS)

    output = io.BytesIO()
    result.save(output, format="WEBP", quality=80)
    if output.tell() > 64000:
        for q in [60, 40, 20]:
            output = io.BytesIO()
            result.save(output, format="WEBP", quality=q)
            if output.tell() <= 64000:
                break
    output.seek(0)
    return output


async def send_menu(update, menu_id):
    text = get_menu_text(menu_id)
    keyboard = build_keyboard(menu_id)

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True
        )


async def nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    menu_id = query.data.replace("nav:", "")
    await send_menu(update, menu_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name or "there"

    if is_new_user(user.id):
        welcome = (
            f"⚗️ <b>The laboratory opens, {first_name}.</b>\n"
            f"{DIV}\n\n"
            "You have entered the sticker alchemy lab.\n\n"
            "◦ Any image → transmuted into a sticker\n"
            "◦ Image + mask → precision cutout ritual\n"
            "◦ Video & GIF forms accepted\n\n"
            "<i>Begin by forging your first pack below.</i>"
        )
        keyboard = build_keyboard("home")
        await update.message.reply_text(welcome, reply_markup=keyboard, parse_mode="HTML")
    else:
        await send_menu(update, "home")


# ── CREATE PACK ──────────────────────────────────────────────

async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"⚗️ <b>FORGE A PACK</b>\n"
        f"{DIV}\n\n"
        "Name the vessel — what shall this pack be called?\n\n"
        "<i>Display title · up to 64 characters.</i>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=cancel_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=cancel_keyboard())
    return WAITING_TITLE


async def create_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if len(title) > 64:
        await update.message.reply_text(
            f"⚠ Name too long — <b>{len(title)}</b> characters.\n"
            "Keep it under 64. Try again:",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )
        return WAITING_TITLE

    context.user_data['newpack_title'] = title
    await update.message.reply_text(
        f"⚗️ <b>{html.escape(title)}</b>\n"
        f"{DIV}\n\n"
        "The vessel is named. Now send the <b>seed sticker</b>.\n\n"
        "◦ Any image, photo, or GIF\n"
        "◦ Videos transmute as animated stickers\n"
        "◦ Or forward an existing sticker",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    return WAITING_STICKER


async def create_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    title = context.user_data.get('newpack_title', 'My Pack')

    file_id, media_type, sticker_format = extract_file_info(update.message)
    if not file_id:
        await update.message.reply_text(
            "⚠ The ingredient is unrecognised — send an image, video, GIF, or sticker.",
            reply_markup=cancel_keyboard()
        )
        return WAITING_STICKER

    progress = await update.message.reply_text("⚗️ <i>Transmuting the vessel...</i>", parse_mode="HTML")

    bot_username = context.bot.username
    suffix = "".join(random.choices(string.ascii_lowercase, k=5))
    pack_name = f"stix_{user.id}_{suffix}_by_{bot_username}"

    try:
        sticker_file = await download_file_bytes(context.bot, file_id)
        if not sticker_file:
            await progress.edit_text("⚠ Download failed. Please try again.")
            return WAITING_STICKER

        if media_type == "image":
            converted = convert_to_sticker(sticker_file)
            if converted:
                sticker_file = converted
        elif media_type == "video":
            await progress.edit_text("⚗️ <i>Distilling the animation...</i>", parse_mode="HTML")
            converted = convert_video_to_sticker(sticker_file)
            if converted:
                sticker_file = converted

        input_sticker = InputSticker(sticker=sticker_file, emoji_list=STICKER_EMOJI, format=sticker_format)

        await context.bot.create_new_sticker_set(
            user_id=user.id,
            name=pack_name,
            title=title,
            stickers=[input_sticker],
        )

        add_pack_to_db(user.id, pack_name, title)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✦ Inscribe More Stickers", callback_data=f"addto_{pack_name}")],
            [InlineKeyboardButton("🔗 Open the Vessel", url=f"https://t.me/addstickers/{pack_name}")],
            [
                InlineKeyboardButton("📖 Grimoire", callback_data="menu_packs"),
                InlineKeyboardButton("✦ Home", callback_data="nav:home"),
            ],
        ])

        await progress.edit_text(
            f"⚗️ <b>Pack forged!</b>\n"
            f"{DIV}\n\n"
            f"<b>{html.escape(title)}</b>\n"
            f"<i>The first sticker is sealed within.</i>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error creating sticker set: {e}")
        err = str(e)
        friendly = "The ingredients were not accepted. Try a PNG or JPG image."
        if "too big" in err.lower():
            friendly = "The ingredient is too large — try a smaller image (under 512px)."
        elif "invalid" in err.lower():
            friendly = "The form was rejected by Telegram. Try a PNG or JPG."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Try Again", callback_data="menu_create")],
            [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        await progress.edit_text(
            f"⚠ <b>The transmutation failed</b>\n"
            f"{DIV}\n\n"
            f"{friendly}",
            parse_mode="HTML",
            reply_markup=keyboard
        )

    context.user_data.clear()
    return ConversationHandler.END


# ── ADD STICKER ──────────────────────────────────────────────

async def addsticker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if update.callback_query:
        await update.callback_query.answer()

    packs = await validate_and_sync_packs(context.bot, user.id)

    if not packs:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚗️ Forge a Pack", callback_data="menu_create")],
            [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        msg = (
            f"✦ <b>INSCRIBE A STICKER</b>\n"
            f"{DIV}\n\n"
            "The grimoire is empty — no vessels exist yet.\n"
            "Forge a pack first!"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
        return ConversationHandler.END

    keyboard_rows = [[InlineKeyboardButton(f"▦  {title}", callback_data=f"pack_{name}")] for name, title in packs]
    keyboard_rows.append([InlineKeyboardButton("✕ Cancel", callback_data="nav:home")])
    context.user_data['user_packs'] = packs

    msg = (
        f"✦ <b>INSCRIBE A STICKER</b>\n"
        f"{DIV}\n\n"
        "Which vessel receives the sticker?"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows))
    else:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows))
    return CHOOSING_PACK


async def addto_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pack_name = query.data.replace("addto_", "")
    packs = get_user_packs(query.from_user.id)
    context.user_data['user_packs'] = packs
    context.user_data['selected_pack'] = pack_name
    await query.edit_message_text(
        f"✦ <b>INSCRIBE A STICKER</b>\n"
        f"{DIV}\n\n"
        "Send the ingredient to bind into this vessel.\n\n"
        "◦ Image, video, GIF, or existing sticker",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    return WAITING_STICKER_ADD


async def addsticker_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pack_name = query.data.replace("pack_", "")
    packs = context.user_data.get('user_packs', [])
    selected_name = next((n for n, _ in packs if n == pack_name), None)

    if not selected_name:
        await query.edit_message_text("Pack not found. Try again.")
        return ConversationHandler.END

    context.user_data['selected_pack'] = selected_name
    pack_title = next((t for n, t in packs if n == pack_name), pack_name)

    await query.edit_message_text(
        f"✦ <b>{html.escape(pack_title)}</b>\n"
        f"{DIV}\n\n"
        "Send the ingredient to seal into this vessel.\n\n"
        "◦ Image, video, GIF, or existing sticker",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    return WAITING_STICKER_ADD


async def addsticker_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    pack_name = context.user_data.get('selected_pack')
    packs = context.user_data.get('user_packs', [])
    pack_title = next((t for n, t in packs if n == pack_name), pack_name)

    file_id, media_type, sticker_format = extract_file_info(update.message)
    if not file_id:
        await update.message.reply_text(
            "⚠ The ingredient is unrecognised — send an image, video, GIF, or sticker.",
            reply_markup=cancel_keyboard()
        )
        return WAITING_STICKER_ADD

    progress = await update.message.reply_text("⚗️ <i>Binding the sticker...</i>", parse_mode="HTML")

    try:
        sticker_file = await download_file_bytes(context.bot, file_id)
        if not sticker_file:
            await progress.edit_text("⚠ Download failed. Please try again.")
            return WAITING_STICKER_ADD

        if media_type == "image":
            converted = convert_to_sticker(sticker_file)
            if converted:
                sticker_file = converted
        elif media_type == "video":
            await progress.edit_text("⚗️ <i>Distilling the animation...</i>", parse_mode="HTML")
            converted = convert_video_to_sticker(sticker_file)
            if converted:
                sticker_file = converted

        input_sticker = InputSticker(sticker=sticker_file, emoji_list=STICKER_EMOJI, format=sticker_format)
        await context.bot.add_sticker_to_set(
            user_id=user.id,
            name=pack_name,
            sticker=input_sticker
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✦ Bind Another", callback_data=f"addto_{pack_name}")],
            [InlineKeyboardButton("🔗 Open the Vessel", url=f"https://t.me/addstickers/{pack_name}")],
            [
                InlineKeyboardButton("📖 Grimoire", callback_data="menu_packs"),
                InlineKeyboardButton("✦ Home", callback_data="nav:home"),
            ],
        ])

        await progress.edit_text(
            f"✦ <b>Sticker sealed</b>\n"
            f"{DIV}\n\n"
            f"<b>{html.escape(pack_title)}</b> grows stronger.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error adding sticker: {e}")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Try Again", callback_data=f"addto_{pack_name}")],
            [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        await progress.edit_text(
            f"⚠ <b>The binding failed</b>\n"
            f"{DIV}\n\n"
            f"<i>{html.escape(str(e))}</i>",
            parse_mode="HTML",
            reply_markup=keyboard
        )

    context.user_data.clear()
    return ConversationHandler.END


# ── MAGIC TOOLS (MASK CUTTER) ───────────────────────────────

async def magic_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    inverted = get_mask_inverted(user.id)
    mode = "⬛ black = keep  ·  ⬜ white = dissolve" if inverted else "⬜ white = keep  ·  ⬛ black = dissolve"

    text = (
        f"◈ <b>THE CUTTING RITUAL</b>\n"
        f"{DIV}\n\n"
        f"<b>Step 1 of 2</b> — Cast the <b>source image</b> into the circle.\n\n"
        f"<i>Oracle mode: {mode}</i>\n"
        f"<i>Reconfigure in ⚙ Oracle settings</i>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=cancel_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=cancel_keyboard())
    return WAITING_SOURCE_IMAGE


async def magic_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, _, _ = extract_file_info(update.message)
    if not file_id:
        await update.message.reply_text(
            "⚠ The form is unrecognised — send a photo or image file.",
            reply_markup=cancel_keyboard()
        )
        return WAITING_SOURCE_IMAGE

    source_bytes = await download_file_bytes(context.bot, file_id)
    if not source_bytes:
        await update.message.reply_text("⚠ The ingredient could not be summoned. Try again.", reply_markup=cancel_keyboard())
        return WAITING_SOURCE_IMAGE

    context.user_data['cut_source'] = source_bytes.getvalue()

    inverted = get_mask_inverted(update.effective_user.id)
    mode = "⬛ black = <b>KEEP</b>  ·  ⬜ white = dissolve" if inverted else "⬜ white = <b>KEEP</b>  ·  ⬛ black = dissolve"

    await update.message.reply_text(
        f"◈ <b>THE CUTTING RITUAL</b>\n"
        f"{DIV}\n\n"
        f"<b>Step 2 of 2</b> — Now present the <b>B&W mask</b>.\n\n"
        f"{mode}",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    return WAITING_MASK_IMAGE


async def magic_mask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, _, _ = extract_file_info(update.message)
    if not file_id:
        await update.message.reply_text(
            "⚠ The mask form is unrecognised — send a black & white image.",
            reply_markup=cancel_keyboard()
        )
        return WAITING_MASK_IMAGE

    mask_bytes = await download_file_bytes(context.bot, file_id)
    if not mask_bytes:
        await update.message.reply_text("⚠ Download failed. Try again.", reply_markup=cancel_keyboard())
        return WAITING_MASK_IMAGE

    source_data = context.user_data.get('cut_source')
    if not source_data:
        await update.message.reply_text(
            "⚠ Source image was lost. Please start over.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Start Over", callback_data="menu_magic")]]),
        )
        context.user_data.clear()
        return ConversationHandler.END

    progress = await update.message.reply_text("◈ <i>The ritual is at work...</i>", parse_mode="HTML")

    try:
        source_io = io.BytesIO(source_data)
        inverted = get_mask_inverted(update.effective_user.id)
        result_webp = apply_mask_to_image(source_io, mask_bytes, inverted=inverted)

        context.user_data['cut_result'] = result_webp.getvalue()

        await progress.delete()
        await update.message.reply_photo(
            photo=io.BytesIO(context.user_data['cut_result']),
            caption=f"◈ <b>The cut is revealed.</b>",
            parse_mode="HTML"
        )

        packs = get_user_packs(update.effective_user.id)
        keyboard_rows = []
        if packs:
            for name, title in packs:
                keyboard_rows.append([InlineKeyboardButton(f"✦ Seal into  {title}", callback_data=f"cutpack_{name}")])
        keyboard_rows.append([InlineKeyboardButton("🜁 Extract the Essence", callback_data="cut_download")])
        keyboard_rows.append([
            InlineKeyboardButton("◈ New Ritual", callback_data="menu_magic"),
            InlineKeyboardButton("✦ Home", callback_data="nav:home"),
        ])

        await update.message.reply_text(
            "What fate for this essence?",
            reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )
    except Exception as e:
        logger.error(f"Error applying mask: {e}")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry Ritual", callback_data="menu_magic")],
            [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        await progress.edit_text(
            f"⚠ <b>The ritual faltered</b>\n"
            f"{DIV}\n\n"
            "Inspect your ingredients.\n"
            "<i>The mask must be a black & white image.</i>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        context.user_data.clear()
        return ConversationHandler.END

    return WAITING_CUT_PACK


async def magic_pack_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    cut_result = context.user_data.get('cut_result')
    if not cut_result:
        await query.edit_message_text(
            "⚠ Sticker data lost. Start over.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Start Over", callback_data="menu_magic")]]),
        )
        context.user_data.clear()
        return ConversationHandler.END

    if data == "cut_download":
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=io.BytesIO(cut_result),
            filename="stixmagic_essence.webp",
            caption="🜁 The essence is extracted — yours to keep."
        )
        await query.edit_message_text(
            "🜁 <b>Essence extracted</b>",
            parse_mode="HTML",
            reply_markup=home_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    if data.startswith("cutpack_"):
        pack_name = data.replace("cutpack_", "")
        user = query.from_user
        packs = get_user_packs(user.id)
        pack_title = next((t for n, t in packs if n == pack_name), pack_name)

        try:
            sticker_file = io.BytesIO(cut_result)
            input_sticker = InputSticker(sticker=sticker_file, emoji_list=STICKER_EMOJI, format="static")
            await context.bot.add_sticker_to_set(
                user_id=user.id,
                name=pack_name,
                sticker=input_sticker
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Open the Vessel", url=f"https://t.me/addstickers/{pack_name}")],
                [InlineKeyboardButton("◈ New Ritual", callback_data="menu_magic"),
                 InlineKeyboardButton("✦ Home", callback_data="nav:home")],
            ])
            await query.edit_message_text(
                f"✦ <b>Bound to {html.escape(pack_title)}</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Error adding cut sticker: {e}")
            await query.edit_message_text(
                f"⚠ <b>The binding failed</b>\n\n<i>{html.escape(str(e))}</i>",
                parse_mode="HTML",
                reply_markup=home_keyboard()
            )

    context.user_data.clear()
    return ConversationHandler.END


# ── PACKS / MANAGE / HELP / ABOUT ───────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "✦ The ritual is dissolved.",
        reply_markup=home_keyboard()
    )
    return ConversationHandler.END


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"📖 <b>THE CRAFT</b>\n"
        f"{DIV}\n\n"
        "<b>⚗️ FORGE A PACK</b>\n"
        "  Name the vessel → seal a sticker inside\n"
        "  → pack is live on Telegram\n\n"
        "<b>✦ INSCRIBE A STICKER</b>\n"
        "  Choose a vessel → bind more stickers within\n\n"
        "<b>◈ THE CUTTING RITUAL</b>\n"
        "  Cast a photo + black & white mask\n"
        "  → receive a clean transparent cutout\n\n"
        "<b>⚙ ORACLE SETTINGS</b>\n"
        "  Reconfigure the mask oracle (white/black = keep)\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("◦ Alchemist's Field Notes", callback_data="nav:tips")],
        [InlineKeyboardButton("◂ Back", callback_data="nav:help"),
         InlineKeyboardButton("✦ Home", callback_data="nav:home")],
    ])

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def show_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"✦ <b>STIX MAGIC</b>\n"
        f"{DIV}\n\n"
        "An alchemist's workshop for Telegram stickers.\n\n"
        "Every image holds a sticker in waiting.\n"
        "We transmute it. We cut. We seal.\n\n"
        "Forge packs. Bind stickers. Perform the ritual.\n\n"
        "<i>stixmagic.com</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 stixmagic.com", url="https://stixmagic.com")],
        [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
    ])

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def manage_stickers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if update.callback_query:
        await update.callback_query.answer()

    packs = await validate_and_sync_packs(context.bot, user.id)

    if not packs:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚗️ Forge a Pack", callback_data="menu_create")],
            [InlineKeyboardButton("◂ Back", callback_data="nav:my_packs"),
             InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        msg = f"⚗️ <b>THE CRUCIBLE</b>\n{DIV}\n\nThe crucible is empty — no vessels to manage."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
        return

    msg = f"⚗️ <b>THE CRUCIBLE</b>\n{DIV}\n\n"
    for idx, (name, title) in enumerate(packs, 1):
        msg += f"{idx}.  <b>{title}</b>\n"

    keyboard_rows = []
    for name, title in packs:
        keyboard_rows.append([
            InlineKeyboardButton(f"✦ {title}", callback_data=f"addto_{name}"),
            InlineKeyboardButton("🜄", callback_data=f"del_{name}"),
        ])
    keyboard_rows.append([InlineKeyboardButton("⚗️ Forge New Pack", callback_data="menu_create")])
    keyboard_rows.append([
        InlineKeyboardButton("◂ Back", callback_data="nav:my_packs"),
        InlineKeyboardButton("✦ Home", callback_data="nav:home"),
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows))
    else:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows))


async def delete_pack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pack_name = query.data.replace("del_", "")
    user = query.from_user
    packs = get_user_packs(user.id)
    pack_title = next((t for n, t in packs if n == pack_name), pack_name)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✓ Yes, remove", callback_data=f"delconfirm_{pack_name}"),
            InlineKeyboardButton("✕ Keep it", callback_data="menu_manage"),
        ]
    ])
    await query.edit_message_text(
        f"⚠ Dissolve <b>{pack_title}</b> from your grimoire?\n\n"
        "<i>This only removes it from Stix Magic — the Telegram vessel stays live.</i>",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def delete_pack_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pack_name = query.data.replace("delconfirm_", "")
    user = query.from_user
    packs = get_user_packs(user.id)
    pack_title = next((t for n, t in packs if n == pack_name), pack_name)

    delete_pack_from_db(user.id, pack_name)

    await query.edit_message_text(
        f"🜄 <b>{pack_title}</b> dissolved from your grimoire.",
        parse_mode="HTML",
        reply_markup=home_keyboard()
    )


async def show_packs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if update.callback_query:
        await update.callback_query.answer()

    packs = await validate_and_sync_packs(context.bot, user.id)

    if not packs:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚗️ Forge a Pack", callback_data="menu_create")],
            [InlineKeyboardButton("◂ Back", callback_data="nav:my_packs"),
             InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        msg = f"📖 <b>YOUR GRIMOIRE</b>\n{DIV}\n\nThe grimoire is empty.\nForge your first vessel!"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
        return

    msg = f"📖 <b>YOUR GRIMOIRE</b>\n{DIV}\n\n"
    for idx, (name, title) in enumerate(packs, 1):
        msg += f"<b>{idx}. {title}</b>\n"

    keyboard_rows = []
    for name, title in packs:
        keyboard_rows.append([InlineKeyboardButton(f"🔗 {title}", url=f"https://t.me/addstickers/{name}")])
    keyboard_rows.append([
        InlineKeyboardButton("⚗️ Forge Pack", callback_data="menu_create"),
        InlineKeyboardButton("✦ Inscribe Sticker", callback_data="menu_addsticker"),
    ])
    keyboard_rows.append([
        InlineKeyboardButton("◂ Back", callback_data="nav:my_packs"),
        InlineKeyboardButton("✦ Home", callback_data="nav:home"),
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows), disable_web_page_preview=True)
    else:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows), disable_web_page_preview=True)


async def settings_mask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    inverted = get_mask_inverted(user.id)
    current = "⬛ Black = keep · ⬜ White = dissolve" if inverted else "⬜ White = keep · ⬛ Black = dissolve"
    toggle_label = "Switch to ⬜ White = keep" if inverted else "Switch to ⬛ Black = keep"

    text = (
        f"◐ <b>THE ORACLE</b>\n"
        f"{DIV}\n\n"
        f"Current mode: <b>{current}</b>\n\n"
        "<i>The oracle decides which color in your mask\n"
        "is preserved and which is dissolved.</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data="toggle_mask")],
        [InlineKeyboardButton("◂ Back", callback_data="nav:settings"),
         InlineKeyboardButton("✦ Home", callback_data="nav:home")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def toggle_mask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    current = get_mask_inverted(user.id)
    set_mask_inverted(user.id, not current)
    await query.answer("Oracle reconfigured ✦")
    await settings_mask(update, context)


# ── SYNC / IMPORT PACK ───────────────────────────────────────

async def sync_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /sync command."""
    if context.args:
        return await _sync_process(update, context, " ".join(context.args))
    await update.message.reply_text(
        f"🔄 <b>SUMMON A PACK</b>\n{DIV}\n\n"
        "Speak the pack name or link to summon it into your grimoire:\n\n"
        "<code>my_pack_name</code>\n"
        "or\n"
        "<code>https://t.me/addstickers/my_pack_name</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✕ Cancel", callback_data="nav:home")]
        ])
    )
    return WAITING_SYNC_NAME


async def sync_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _sync_process(update, context, update.message.text.strip())


async def _sync_process(update: Update, context: ContextTypes.DEFAULT_TYPE, pack_input: str):
    """Validate the pack name/link and add to DB if found."""
    pack_name = pack_input.strip().rstrip("/")
    if "t.me/addstickers/" in pack_name:
        pack_name = pack_name.split("t.me/addstickers/")[-1].strip().rstrip("/")

    user = update.effective_user
    status_msg = await update.message.reply_text("🔄 <i>Consulting the archives...</i>", parse_mode="HTML")

    try:
        ss = await context.bot.get_sticker_set(pack_name)
    except Exception:
        await status_msg.edit_text(
            f"⚠ <b>Pack not found in the archives</b>\n{DIV}\n\n"
            f"No vessel named <code>{html.escape(pack_name)}</code> exists on Telegram.\n\n"
            "Try speaking the name again, or /cancel to abandon.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✕ Cancel", callback_data="nav:home")]
            ])
        )
        return WAITING_SYNC_NAME

    existing = get_user_packs(user.id)
    if any(n == pack_name for n, _ in existing):
        await status_msg.edit_text(
            f"✦ <b>{html.escape(ss.title)}</b> is already bound to your grimoire.",
            parse_mode="HTML",
            reply_markup=home_keyboard()
        )
        return ConversationHandler.END

    add_pack_to_db(user.id, pack_name, ss.title)
    await status_msg.edit_text(
        f"⚗️ <b>Pack summoned!</b>\n{DIV}\n\n"
        f"<b>{html.escape(ss.title)}</b> has been bound to your grimoire.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Open the Vessel", url=f"https://t.me/addstickers/{pack_name}")],
            [InlineKeyboardButton("⚗️ The Crucible", callback_data="menu_manage")],
            [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
    )
    return ConversationHandler.END


# ── ANIMATED TASK FEEDBACK ────────────────────────────────────

async def send_working_animation(update: Update):
    """Send a 'working…' placeholder while the bot processes a request.

    Returns the sent Message so the caller can delete it when done.
    The placeholder keeps the chat informative without permanent clutter.
    """
    return await update.effective_message.reply_text(
        "🧙 <i>working on it…</i>",
        parse_mode="HTML"
    )


async def delete_working_animation(bot, chat_id: int, message_id: int):
    """Delete the working-animation placeholder sent by send_working_animation."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # already deleted or not found — safe to ignore


# ── DRAFT VAULT COMMANDS ──────────────────────────────────────

async def mydrafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's pending draft stickers.

    Drafts are generated stickers awaiting review.  From here the user
    can approve, retry, or reject each one.
    """
    user = update.effective_user
    drafts = get_user_drafts(user.id, status='draft')

    if not drafts:
        text = (
            f"🗂 <b>DRAFT VAULT</b>\n{DIV}\n\n"
            "No pending drafts — the vault is empty.\n\n"
            "<i>Generate a sticker to see it here first.</i>"
        )
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=home_keyboard()
        )
        return

    text = (
        f"🗂 <b>DRAFT VAULT</b>\n{DIV}\n\n"
        f"You have <b>{len(drafts)}</b> pending draft(s).\n\n"
        "<i>Use the buttons below to approve, retry, or reject each draft.</i>"
    )
    keyboard_rows = []
    for draft_id, file_id, status, created_at, expires_at in drafts:
        label = f"Draft #{draft_id}"
        keyboard_rows.append([
            InlineKeyboardButton(f"✓ Approve #{draft_id}", callback_data=f"draft_approve_{draft_id}"),
            InlineKeyboardButton(f"✕ Reject", callback_data=f"draft_reject_{draft_id}"),
        ])
        keyboard_rows.append([
            InlineKeyboardButton(f"🔄 Retry #{draft_id}", callback_data=f"draft_retry_{draft_id}"),
            InlineKeyboardButton(f"💾 Save later", callback_data=f"draft_save_{draft_id}"),
        ])
    keyboard_rows.append([InlineKeyboardButton("✦ Home", callback_data="nav:home")])

    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard_rows)
    )


async def myapproved_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's approved stickers ready for publishing."""
    user = update.effective_user
    approved = get_user_drafts(user.id, status='approved')

    if not approved:
        text = (
            f"✅ <b>APPROVED STICKERS</b>\n{DIV}\n\n"
            "No approved stickers yet.\n\n"
            "<i>Approve drafts from /mydrafts to see them here.</i>"
        )
    else:
        text = (
            f"✅ <b>APPROVED STICKERS</b>\n{DIV}\n\n"
            f"<b>{len(approved)}</b> sticker(s) ready to publish.\n\n"
            "<i>These stickers can be added to your packs or collections.</i>"
        )

    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=home_keyboard()
    )


async def trash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's rejected and expired stickers (the trash bin)."""
    user = update.effective_user
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    user_row = get_or_create_user(user.id, user.username)
    c.execute(
        "SELECT id, status, created_at FROM sticker_drafts "
        "WHERE user_id = ? AND status IN ('rejected', 'expired') ORDER BY created_at DESC",
        (user_row['id'],)
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        text = (
            f"🗑 <b>TRASH</b>\n{DIV}\n\n"
            "Nothing in the trash — all clean."
        )
    else:
        text = (
            f"🗑 <b>TRASH</b>\n{DIV}\n\n"
            f"<b>{len(rows)}</b> discarded sticker(s).\n\n"
        )
        for draft_id, status, created_at in rows[:10]:
            text += f"◦ #{draft_id} — <i>{status}</i> on {created_at[:10]}\n"
        if len(rows) > 10:
            text += f"\n<i>…and {len(rows) - 10} more.</i>"

    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=home_keyboard()
    )


async def catalog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the available sticker style catalog.

    Displays all active styles from the catalog_styles table, grouped by
    plan tier so users know which styles are available on their plan.
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT name, slug, description, category, plan_access "
        "FROM catalog_styles WHERE status = 'active' ORDER BY plan_access, name"
    )
    styles = c.fetchall()
    conn.close()

    if not styles:
        text = (
            f"🎨 <b>STYLE CATALOG</b>\n{DIV}\n\n"
            "No styles available yet — check back soon!\n\n"
            "<i>New styles are added regularly for all plans.</i>"
        )
    else:
        text = f"🎨 <b>STYLE CATALOG</b>\n{DIV}\n\n"
        for name, slug, description, category, plan_access in styles:
            tier_icon = {"free": "🆓", "premium": "⭐", "pro": "💎"}.get(plan_access, "🆓")
            text += f"{tier_icon} <b>{name}</b>"
            if category:
                text += f" <i>({category})</i>"
            text += "\n"
            if description:
                text += f"   {description}\n"
        text += "\n<i>Use /plans to see what each tier includes.</i>"

    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=home_keyboard()
    )


async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the available subscription plans and their creation quotas."""
    user = update.effective_user
    current_plan = get_user_plan(user.id)
    _, remaining = check_creation_limit(user.id)

    text = (
        f"💎 <b>PLANS</b>\n{DIV}\n\n"
        f"Your current plan: <b>{current_plan.upper()}</b>\n"
        f"Remaining creations this period: <b>{remaining}</b>\n\n"
        "──────────────────────\n"
        "🆓 <b>Free</b>\n"
        "  · 3 creations per day\n"
        "  · 10 drafts\n"
        "  · Basic styles\n\n"
        "⭐ <b>Premium</b>\n"
        "  · 50 creations per month\n"
        "  · 100 drafts\n"
        "  · All styles\n\n"
        "💎 <b>Pro</b>\n"
        "  · 300 creations per month\n"
        "  · Unlimited drafts\n"
        "  · Priority processing\n"
        "──────────────────────\n\n"
        "<i>Premium and Pro plans coming soon!</i>"
    )

    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=home_keyboard()
    )


# ── DRAFT ACTION CALLBACKS ────────────────────────────────────

async def draft_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button actions from the draft vault.

    Supported actions:
      draft_approve_<id>   — move draft to 'approved'
      draft_reject_<id>    — move draft to 'rejected'
      draft_retry_<id>     — (placeholder) re-generate this draft
      draft_save_<id>      — keep in vault, extend expiry by DRAFT_EXPIRY_DAYS
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("draft_approve_"):
        draft_id = int(data.split("draft_approve_")[1])
        update_draft_status(draft_id, "approved")
        await query.edit_message_text(
            f"✅ Draft <b>#{draft_id}</b> approved!\n\n"
            "<i>It's ready to be added to a pack or collection.</i>",
            parse_mode="HTML",
            reply_markup=home_keyboard()
        )

    elif data.startswith("draft_reject_"):
        draft_id = int(data.split("draft_reject_")[1])
        update_draft_status(draft_id, "rejected")
        await query.edit_message_text(
            f"🗑 Draft <b>#{draft_id}</b> rejected and moved to trash.",
            parse_mode="HTML",
            reply_markup=home_keyboard()
        )

    elif data.startswith("draft_retry_"):
        draft_id = int(data.split("draft_retry_")[1])
        await query.edit_message_text(
            f"🔄 Draft <b>#{draft_id}</b>: retry coming soon!\n\n"
            "<i>Re-generation will be available in Phase 2.</i>",
            parse_mode="HTML",
            reply_markup=home_keyboard()
        )

    elif data.startswith("draft_save_"):
        draft_id = int(data.split("draft_save_")[1])
        new_expiry = (_utcnow() + timedelta(days=DRAFT_EXPIRY_DAYS)).isoformat()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "UPDATE sticker_drafts SET expires_at = ?, updated_at = datetime('now') WHERE id = ?",
            (new_expiry, draft_id)
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"💾 Draft <b>#{draft_id}</b> saved for later — expiry extended.",
            parse_mode="HTML",
            reply_markup=home_keyboard()
        )


# ── CALLBACK ROUTER ──────────────────────────────────────────

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "menu_manage":
        await manage_stickers(update, context)
    elif data == "menu_help_detail":
        await show_help(update, context)
    elif data == "menu_packs":
        await show_packs(update, context)
    elif data == "menu_about":
        await show_about(update, context)
    elif data == "settings_mask":
        await settings_mask(update, context)
    elif data == "toggle_mask":
        await toggle_mask(update, context)
    elif data.startswith("del_"):
        await delete_pack_callback(update, context)
    elif data.startswith("delconfirm_"):
        await delete_pack_confirm(update, context)
    elif data.startswith("draft_"):
        await draft_action_callback(update, context)
    elif data == "menu_mydrafts":
        await query.answer()
        await query.message.reply_text(
            "Use /mydrafts to view your pending drafts.",
            reply_markup=home_keyboard()
        )
    elif data == "menu_myapproved":
        await query.answer()
        await query.message.reply_text(
            "Use /myapproved to view your approved stickers.",
            reply_markup=home_keyboard()
        )
    elif data == "menu_trash":
        await query.answer()
        await query.message.reply_text(
            "Use /trash to view rejected and expired stickers.",
            reply_markup=home_keyboard()
        )
    elif data == "menu_catalog":
        await query.answer()
        await query.message.reply_text(
            "Use /catalog to browse available styles.",
            reply_markup=home_keyboard()
        )
    elif data == "menu_plans":
        await query.answer()
        await query.message.reply_text(
            "Use /plans to view subscription plans.",
            reply_markup=home_keyboard()
        )


# ── MAIN ─────────────────────────────────────────────────────

def main():
    raw_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not raw_token:
        logger.error("No TELEGRAM_BOT_TOKEN set. Add it in Secrets.")
        return

    token_match = re.search(r'\d+:[A-Za-z0-9_-]+', raw_token)
    if not token_match:
        logger.error("Invalid token format in TELEGRAM_BOT_TOKEN.")
        return

    token = token_match.group(0)

    from menus import MINIAPP_URL

    async def post_init(app):
        if MINIAPP_URL:
            try:
                await app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="✦ Mini App", web_app=WebAppInfo(url=MINIAPP_URL))
                )
                logger.info(f"Menu button set to Mini App: {MINIAPP_URL}")
            except Exception as e:
                logger.warning(f"Could not set menu button: {e}")

    application = Application.builder().token(token).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("packs", show_packs))
    application.add_handler(CommandHandler("manage", manage_stickers))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("about", show_about))
    # Draft vault & lifecycle commands
    application.add_handler(CommandHandler("mydrafts", mydrafts_command))
    application.add_handler(CommandHandler("myapproved", myapproved_command))
    application.add_handler(CommandHandler("trash", trash_command))
    # Catalog & plan commands
    application.add_handler(CommandHandler("catalog", catalog_command))
    application.add_handler(CommandHandler("plans", plans_command))

    create_conv = ConversationHandler(
        entry_points=[CommandHandler("create", create_start), CallbackQueryHandler(create_start, pattern="^menu_create$")],
        states={
            WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_title)],
            WAITING_STICKER: [MessageHandler(filters.ALL & ~filters.COMMAND, create_sticker)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(create_conv)

    addsticker_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addsticker", addsticker_start),
            CallbackQueryHandler(addsticker_start, pattern="^menu_addsticker$"),
            CallbackQueryHandler(addto_direct, pattern="^addto_"),
        ],
        states={
            CHOOSING_PACK: [CallbackQueryHandler(addsticker_choose, pattern="^pack_")],
            WAITING_STICKER_ADD: [MessageHandler(filters.ALL & ~filters.COMMAND, addsticker_receive)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(addsticker_conv)

    magic_conv = ConversationHandler(
        entry_points=[CommandHandler("magic", magic_start), CallbackQueryHandler(magic_start, pattern="^menu_magic$")],
        states={
            WAITING_SOURCE_IMAGE: [MessageHandler(filters.PHOTO | filters.Document.ALL, magic_source)],
            WAITING_MASK_IMAGE: [MessageHandler(filters.PHOTO | filters.Document.ALL, magic_mask)],
            WAITING_CUT_PACK: [CallbackQueryHandler(magic_pack_action, pattern="^(cutpack_|cut_download)")]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(magic_conv)

    sync_conv = ConversationHandler(
        entry_points=[
            CommandHandler("sync", sync_start),
            CallbackQueryHandler(sync_start, pattern="^menu_sync$"),
        ],
        states={
            WAITING_SYNC_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sync_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(sync_conv)

    application.add_handler(CallbackQueryHandler(nav_callback, pattern="^nav:"))
    application.add_handler(CallbackQueryHandler(menu_callback))

    from api import run_api
    web_thread = threading.Thread(target=run_api, daemon=True)
    web_thread.start()
    logger.info("API + landing page serving on port 5000")

    # Start the background cleanup worker (expires stale drafts daily)
    cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
    cleanup_thread.start()
    logger.info("Draft cleanup worker started")

    logger.info("Stix Magic bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
