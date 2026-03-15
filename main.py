import html
import io
import logging
import os
import random
import re
import string
import threading

from telegram import (
    InputSticker, InlineKeyboardButton, InlineKeyboardMarkup,
    MenuButtonWebApp, Update, WebAppInfo,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from infra.db import (
    add_pack as add_pack_to_db,
    delete_pack as delete_pack_from_db,
    get_mask_inverted,
    get_user_packs,
    init_db,
    is_new_user,
    log_event,
    set_mask_inverted,
    update_pack_title as update_pack_title_in_db,
)
from domain.media import (
    async_apply_mask_to_image,
    async_convert_to_sticker,
    async_convert_video_to_sticker,
    download_file_bytes,
    extract_file_info,
)
from menus import build_keyboard, get_menu_text

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

init_db()

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
            converted = await async_convert_to_sticker(sticker_file)
            if converted:
                sticker_file = converted
        elif media_type == "video":
            await progress.edit_text("⚗️ <i>Distilling the animation...</i>", parse_mode="HTML")
            converted = await async_convert_video_to_sticker(sticker_file)
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
        log_event(user.id, "pack_created", pack_name)

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
            converted = await async_convert_to_sticker(sticker_file)
            if converted:
                sticker_file = converted
        elif media_type == "video":
            await progress.edit_text("⚗️ <i>Distilling the animation...</i>", parse_mode="HTML")
            converted = await async_convert_video_to_sticker(sticker_file)
            if converted:
                sticker_file = converted

        input_sticker = InputSticker(sticker=sticker_file, emoji_list=STICKER_EMOJI, format=sticker_format)
        await context.bot.add_sticker_to_set(
            user_id=user.id,
            name=pack_name,
            sticker=input_sticker
        )
        log_event(user.id, "sticker_added", pack_name)

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
        result_webp = await async_apply_mask_to_image(source_io, mask_bytes, inverted=inverted)

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


# ── CALLBACK ROUTER ──────────────────────────────────────────

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    _dispatch = {
        "menu_manage": manage_stickers,
        "menu_help_detail": show_help,
        "menu_packs": show_packs,
        "menu_about": show_about,
        "settings_mask": settings_mask,
        "toggle_mask": toggle_mask,
    }

    if data in _dispatch:
        await _dispatch[data](update, context)
    elif data.startswith("del_"):
        await delete_pack_callback(update, context)
    elif data.startswith("delconfirm_"):
        await delete_pack_confirm(update, context)


# ── STATUS COMMAND ────────────────────────────────────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only /status command: show basic platform health metrics."""
    import time
    from infra.db import get_event_counts
    from infra.db import db_conn

    with db_conn() as conn:
        users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM packs").fetchone()[0]
        packs = conn.execute("SELECT COUNT(*) FROM packs").fetchone()[0]

    events = get_event_counts(10)
    event_lines = "\n".join(f"  {e['event']}: {e['count']}" for e in events) or "  (none)"

    text = (
        f"⚙ <b>PLATFORM STATUS</b>\n"
        f"{DIV}\n\n"
        f"<b>Users with packs:</b> {users}\n"
        f"<b>Total packs:</b> {packs}\n\n"
        f"<b>Top events:</b>\n{event_lines}\n\n"
        f"<i>UTC {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ── GLOBAL ERROR HANDLER ──────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled exceptions raised inside bot handlers."""
    logger.error("Unhandled exception in update %s", update, exc_info=context.error)


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
    application.add_handler(CommandHandler("status", status))

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
    application.add_error_handler(error_handler)

    from api import run_api
    web_thread = threading.Thread(target=run_api, daemon=True)
    web_thread.start()
    logger.info("API + landing page serving on port 5000")

    logger.info("Stix Magic bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
