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
from PIL import Image, ImageOps
from telegram import Update, InputSticker, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
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

def is_new_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM packs WHERE user_id = ?', (user_id,))
    packs = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM user_settings WHERE user_id = ?', (user_id,))
    settings = c.fetchone()[0]
    conn.close()
    return packs == 0 and settings == 0


WAITING_TITLE, WAITING_STICKER = range(2)
CHOOSING_PACK, WAITING_STICKER_ADD = range(2, 4)
WAITING_SOURCE_IMAGE, WAITING_MASK_IMAGE, WAITING_CUT_PACK = range(4, 7)

STICKER_EMOJI = ["✨"]

DIV = "✦ ─────────────── ✦"

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
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True
        )
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
            f"🔮 <b>Welcome, {first_name}!</b>\n"
            f"{DIV}\n\n"
            "✨ You've just entered the <b>Stix Magic</b> workshop — "
            "where ordinary photos become Telegram stickers!\n\n"
            "Here's all you need to know:\n\n"
            "1️⃣ Tap <b>🟣 CREATE PACK</b> and give your pack a name\n"
            "2️⃣ Send any photo or video — it becomes a sticker instantly!\n"
            "3️⃣ Add more stickers, share with friends, or use <b>⚗️ Magic Cut</b> "
            "to remove backgrounds like a wizard 🧙\n\n"
            "<i>Not sure where to start? Tap 🟠 HELP — START HERE below!</i>"
        )
        keyboard = build_keyboard("home")
        await update.message.reply_text(welcome, reply_markup=keyboard, parse_mode="HTML")
    else:
        await send_menu(update, "home")


# ── CREATE PACK ──────────────────────────────────────────────

async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🟣 <b>CREATE A NEW STICKER PACK</b>\n"
        f"{DIV}\n\n"
        "Easy! Just tell me what you want to call it.\n\n"
        "📝 Type the <b>display title</b> for your pack:\n"
        "<i>(e.g. \"My Cool Stickers\" — up to 64 characters)</i>"
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
        f"✨ Great name — <b>{html.escape(title)}</b>!\n"
        f"{DIV}\n\n"
        "Now send me the <b>first sticker image</b> for this pack.\n\n"
        "📸 You can send:\n"
        "◦ A regular photo or image file (PNG, JPG)\n"
        "◦ A GIF or short video (becomes an animated sticker)\n"
        "◦ An existing Telegram sticker\n\n"
        "<i>Don't worry — you can always add more later!</i>",
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
            "⚠ Send an image, video, GIF, or sticker.",
            reply_markup=cancel_keyboard()
        )
        return WAITING_STICKER

    progress = await update.message.reply_text("✦ <i>Creating your pack...</i>", parse_mode="HTML")

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
            await progress.edit_text("✦ <i>Converting video...</i>", parse_mode="HTML")
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
            [InlineKeyboardButton("➕ Add More Stickers", callback_data=f"addto_{pack_name}")],
            [InlineKeyboardButton("🔗 Open Pack", url=f"https://t.me/addstickers/{pack_name}")],
            [
                InlineKeyboardButton("▦ My Packs", callback_data="menu_packs"),
                InlineKeyboardButton("✦ Home", callback_data="nav:home"),
            ],
        ])

        await progress.edit_text(
            f"🎉 <b>Pack created!</b>\n"
            f"{DIV}\n\n"
            f"<b>{html.escape(title)}</b> is now live on Telegram! 🚀\n\n"
            "<i>Tap ➕ Add More Stickers to keep building,\n"
            "or 🔗 Open Pack to see it right now.</i>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error creating sticker set: {e}")
        err = str(e)
        friendly = "The file might be too large or an unsupported format. Try a PNG or JPG image."
        if "too big" in err.lower():
            friendly = "File is too large. Try a smaller image (under 512px)."
        elif "invalid" in err.lower():
            friendly = "The file format wasn't accepted. Try a PNG or JPG."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Try Again", callback_data="menu_create")],
            [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        await progress.edit_text(
            f"⚠️ <b>Something went wrong</b>\n"
            f"{DIV}\n\n"
            f"{friendly}\n\n"
            "<i>Tip: use a PNG or JPG image, ideally 512 × 512 px.</i>",
            parse_mode="HTML",
            reply_markup=keyboard
        )

    context.user_data.clear()
    return ConversationHandler.END


# ── ADD STICKER ──────────────────────────────────────────────

async def addsticker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    packs = get_user_packs(user.id)

    if update.callback_query:
        await update.callback_query.answer()

    if not packs:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬡ Create a Pack", callback_data="menu_create")],
            [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        msg = (
            f"➕ <b>ADD STICKER</b>\n"
            f"{DIV}\n\n"
            "You don't have any packs yet!\n\n"
            "<i>Create your first pack — it only takes a few seconds. 🔮</i>"
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
        f"➕ <b>ADD STICKER</b>\n"
        f"{DIV}\n\n"
        "👇 Which pack do you want to add to?"
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
        f"➕ <b>ADD STICKER</b>\n"
        f"{DIV}\n\n"
        "Send me the sticker image to add.\n\n"
        "📸 Photo, image file, GIF, video, or an existing sticker",
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
        f"➕ <b>{html.escape(pack_title)}</b>\n"
        f"{DIV}\n\n"
        "Send me the image to add as a sticker.\n\n"
        "📸 Photo, image file, GIF, video, or an existing sticker",
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
            "⚠ Send an image, video, GIF, or sticker.",
            reply_markup=cancel_keyboard()
        )
        return WAITING_STICKER_ADD

    progress = await update.message.reply_text("✦ <i>Adding sticker...</i>", parse_mode="HTML")

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
            await progress.edit_text("✦ <i>Converting video...</i>", parse_mode="HTML")
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
            [InlineKeyboardButton("➕ Add Another", callback_data=f"addto_{pack_name}")],
            [InlineKeyboardButton("🔗 Open Pack", url=f"https://t.me/addstickers/{pack_name}")],
            [
                InlineKeyboardButton("▦ My Packs", callback_data="menu_packs"),
                InlineKeyboardButton("✦ Home", callback_data="nav:home"),
            ],
        ])

        await progress.edit_text(
            f"✨ <b>Sticker added!</b>\n"
            f"{DIV}\n\n"
            f"<b>{html.escape(pack_title)}</b> is growing! 🎉\n\n"
            "<i>Keep adding more or open the pack to share it.</i>",
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
            f"⚠ <b>Couldn't add sticker</b>\n"
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
    mode = "⬛ black = keep  ·  ⬜ white = remove" if inverted else "⬜ white = keep  ·  ⬛ black = remove"

    text = (
        f"⚗️ <b>MAGIC CUT — Step 1 of 2</b>\n"
        f"{DIV}\n\n"
        "This spell removes the background from your photo!\n\n"
        "📸 First, send me the <b>photo you want to cut</b>.\n\n"
        f"<i>Mask mode: {mode}</i>\n"
        f"<i>(Change this in ⚙️ Settings → Mask Mode)</i>"
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
            "⚠ Send a photo or image file.",
            reply_markup=cancel_keyboard()
        )
        return WAITING_SOURCE_IMAGE

    source_bytes = await download_file_bytes(context.bot, file_id)
    if not source_bytes:
        await update.message.reply_text("⚠ Download failed. Try again.", reply_markup=cancel_keyboard())
        return WAITING_SOURCE_IMAGE

    context.user_data['cut_source'] = source_bytes.getvalue()

    inverted = get_mask_inverted(update.effective_user.id)
    mode = "⬛ black = <b>KEEP</b>  ·  ⬜ white = remove" if inverted else "⬜ white = <b>KEEP</b>  ·  ⬛ black = remove"

    await update.message.reply_text(
        f"⚗️ <b>MAGIC CUT — Step 2 of 2</b>\n"
        f"{DIV}\n\n"
        "Now send me the <b>black‑and‑white mask</b>.\n\n"
        f"{mode}\n\n"
        "<i>The white/black areas in your mask tell the bot\n"
        "which parts of the photo to keep or remove.</i>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    return WAITING_MASK_IMAGE


async def magic_mask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, _, _ = extract_file_info(update.message)
    if not file_id:
        await update.message.reply_text(
            "⚠ Send a black‑and‑white mask image.",
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

    progress = await update.message.reply_text("✦ <i>The wizard is cutting...</i>", parse_mode="HTML")

    try:
        source_io = io.BytesIO(source_data)
        inverted = get_mask_inverted(update.effective_user.id)
        result_webp = apply_mask_to_image(source_io, mask_bytes, inverted=inverted)

        context.user_data['cut_result'] = result_webp.getvalue()

        await progress.delete()
        await update.message.reply_photo(
            photo=io.BytesIO(context.user_data['cut_result']),
            caption=f"✨ <b>Preview</b> — how does it look?",
            parse_mode="HTML"
        )

        packs = get_user_packs(update.effective_user.id)
        keyboard_rows = []
        if packs:
            for name, title in packs:
                keyboard_rows.append([InlineKeyboardButton(f"➕ Add to  {title}", callback_data=f"cutpack_{name}")])
        keyboard_rows.append([InlineKeyboardButton("💾 Download File", callback_data="cut_download")])
        keyboard_rows.append([
            InlineKeyboardButton("🔄 Start Over", callback_data="menu_magic"),
            InlineKeyboardButton("✦ Home", callback_data="nav:home"),
        ])

        await update.message.reply_text(
            "✨ What would you like to do with your cut‑out?",
            reply_markup=InlineKeyboardMarkup(keyboard_rows)
        )
    except Exception as e:
        logger.error(f"Error applying mask: {e}")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Try Again", callback_data="menu_magic")],
            [InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        await progress.edit_text(
            f"⚠️ <b>Magic Cut failed</b>\n"
            f"{DIV}\n\n"
            "Please make sure both images are valid.\n"
            "<i>The mask must be a plain black‑and‑white image (no colors).</i>",
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
            filename="stixmagic_sticker.webp",
            caption="✦ Your sticker — ready to use"
        )
        await query.edit_message_text(
            "💾 <b>Downloaded</b>",
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
                [InlineKeyboardButton("🔗 Open Pack", url=f"https://t.me/addstickers/{pack_name}")],
                [InlineKeyboardButton("◈ New Cut", callback_data="menu_magic"),
                 InlineKeyboardButton("✦ Home", callback_data="nav:home")],
            ])
            await query.edit_message_text(
                f"✦ <b>Added to {html.escape(pack_title)}</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Error adding cut sticker: {e}")
            await query.edit_message_text(
                f"⚠ <b>Couldn't add sticker</b>\n\n<i>{html.escape(str(e))}</i>",
                parse_mode="HTML",
                reply_markup=home_keyboard()
            )

    context.user_data.clear()
    return ConversationHandler.END


# ── PACKS / MANAGE / HELP / ABOUT ───────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "✦ Cancelled.",
        reply_markup=home_keyboard()
    )
    return ConversationHandler.END


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"📖 <b>HOW IT WORKS</b>\n"
        f"{DIV}\n\n"
        "<b>🟣 CREATE A STICKER PACK</b>\n"
        "  1. Tap Create Pack and type a title\n"
        "  2. Send any photo or video\n"
        "  3. Your pack is live on Telegram! 🎉\n\n"
        "<b>➕ ADD MORE STICKERS</b>\n"
        "  Tap Add Sticker, pick your pack,\n"
        "  then send the image to add.\n\n"
        "<b>⚗️ MAGIC CUT</b>\n"
        "  1. Send your <b>subject photo</b>\n"
        "  2. Send a <b>black‑and‑white mask</b>\n"
        "     (white = keep, black = remove)\n"
        "  3. Get a clean transparent cut‑out!\n\n"
        "<b>⚙️ SETTINGS</b>\n"
        "  Flip mask colors if your cut‑outs\n"
        "  look inverted.\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💡 Quick Tips", callback_data="nav:tips")],
        [InlineKeyboardButton("◂ Back", callback_data="nav:help"),
         InlineKeyboardButton("🔮 Home", callback_data="nav:home")],
    ])

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def show_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🔮 <b>STIX MAGIC</b>\n"
        f"{DIV}\n\n"
        "✨ Your personal sticker alchemy workshop.\n\n"
        "Transform any photo, video, or GIF into a\n"
        "Telegram sticker in seconds — no apps, no\n"
        "editing skills needed. Pure magic. 🧙\n\n"
        "<i>stixmagic.com</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 stixmagic.com", url="https://stixmagic.com")],
        [InlineKeyboardButton("🔮 Home", callback_data="nav:home")],
    ])

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def manage_stickers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    packs = get_user_packs(user.id)

    if update.callback_query:
        await update.callback_query.answer()

    if not packs:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬡ Create a Pack", callback_data="menu_create")],
            [InlineKeyboardButton("◂ Back", callback_data="nav:my_packs"),
             InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        msg = f"⚡ <b>MANAGE</b>\n{DIV}\n\nNo packs yet."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
        return

    msg = f"⚡ <b>MANAGE</b>\n{DIV}\n\n"
    for idx, (name, title) in enumerate(packs, 1):
        msg += f"{idx}.  <b>{title}</b>\n"

    keyboard_rows = []
    for name, title in packs:
        keyboard_rows.append([
            InlineKeyboardButton(f"➕ {title}", callback_data=f"addto_{name}"),
            InlineKeyboardButton("🗑", callback_data=f"del_{name}"),
        ])
    keyboard_rows.append([InlineKeyboardButton("⬡ New Pack", callback_data="menu_create")])
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
        f"⚠ Remove <b>{pack_title}</b> from your list?\n\n"
        "<i>This only removes it from Stix Magic — the Telegram pack stays live.</i>",
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
        f"✦ <b>{pack_title}</b> removed from your list.",
        parse_mode="HTML",
        reply_markup=home_keyboard()
    )


async def show_packs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    packs = get_user_packs(user.id)

    if update.callback_query:
        await update.callback_query.answer()

    if not packs:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬡ Create a Pack", callback_data="menu_create")],
            [InlineKeyboardButton("◂ Back", callback_data="nav:my_packs"),
             InlineKeyboardButton("✦ Home", callback_data="nav:home")],
        ])
        msg = f"▦ <b>YOUR PACKS</b>\n{DIV}\n\nNothing here yet.\nCreate your first pack!"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
        return

    msg = f"▦ <b>YOUR PACKS</b>\n{DIV}\n\n"
    for idx, (name, title) in enumerate(packs, 1):
        msg += f"<b>{idx}. {title}</b>\n"

    keyboard_rows = []
    for name, title in packs:
        keyboard_rows.append([InlineKeyboardButton(f"🔗 {title}", url=f"https://t.me/addstickers/{name}")])
    keyboard_rows.append([
        InlineKeyboardButton("⬡ New Pack", callback_data="menu_create"),
        InlineKeyboardButton("➕ Add Sticker", callback_data="menu_addsticker"),
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
    current = "⬛ Black = keep" if inverted else "⬜ White = keep"
    toggle_label = "Switch to ⬜ White = keep" if inverted else "Switch to ⬛ Black = keep"

    text = (
        f"⚙️ <b>MASK MODE</b>\n"
        f"{DIV}\n\n"
        f"Current setting: <b>{current}</b>\n\n"
        "This controls which color in your mask image\n"
        "gets <b>kept</b> when using ⚗️ Magic Cut.\n\n"
        "◦ <b>White = keep</b> — paint white over the subject\n"
        "◦ <b>Black = keep</b> — paint black over the subject\n\n"
        "<i>If your cut‑outs look inside‑out, tap the button below to flip it.</i>"
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
    await query.answer("Switched!")
    await settings_mask(update, context)


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

    application.add_handler(CallbackQueryHandler(nav_callback, pattern="^nav:"))
    application.add_handler(CallbackQueryHandler(menu_callback))

    from api import run_api
    web_thread = threading.Thread(target=run_api, daemon=True)
    web_thread.start()
    logger.info("API + landing page serving on port 5000")

    logger.info("Stix Magic bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
