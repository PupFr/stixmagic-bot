import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

DIV = "✦ ─────────────── ✦"

def _resolve_miniapp_url():
    url = os.environ.get("MINIAPP_URL", "")
    if not url:
        domains = os.environ.get("REPLIT_DOMAINS", "")
        if domains:
            url = f"https://{domains.split(',')[0]}/miniapp"
    return url

MINIAPP_URL = _resolve_miniapp_url()

MENU_STRUCTURE = {

    "home": {
        "header": "🔮 <b>STIX MAGIC</b>",
        "body": (
            "<i>✨ Your sticker alchemy workshop ✨</i>\n\n"
            "Turn <b>any photo or video</b> into a Telegram sticker in seconds.\n\n"
            "🟣 <b>Create</b> — start a brand-new sticker pack\n"
            "⚗️ <b>Magic Cut</b> — cut a subject out of any photo\n"
            "🔵 <b>My Packs</b> — see all your packs\n"
            "🟠 <b>Help</b> — new here? start here!\n"
        ),
        "buttons": [
            [
                {"label": "🟣 CREATE PACK", "action": "menu_create"},
                {"label": "⚗️ MAGIC CUT", "action": "menu_magic"},
            ],
            [
                {"label": "🔵 MY PACKS", "nav": "my_packs"},
                {"label": "🔵 SETTINGS", "nav": "settings"},
            ],
            [
                {"label": "🟠 HELP — START HERE", "nav": "help"},
                {"label": "🟠 ABOUT", "action": "menu_about"},
            ],
            [
                {"label": "✨ OPEN MINI APP", "web_app": True},
            ],
        ],
        "parent": None,
    },

    "my_packs": {
        "header": "🔵 <b>MY PACKS</b>",
        "body": (
            "<i>Your personal sticker collection 📦</i>\n\n"
            "Here you can <b>view</b>, <b>add stickers to</b>, or\n"
            "<b>manage</b> all your packs.\n\n"
            "🔗 Tap any pack link to open it in Telegram.\n"
        ),
        "buttons": [
            [
                {"label": "👁 VIEW ALL PACKS", "action": "menu_packs"},
                {"label": "➕ ADD STICKER", "action": "menu_addsticker"},
            ],
            [{"label": "⚡ MANAGE PACKS", "action": "menu_manage"}],
        ],
        "parent": "home",
    },

    "settings": {
        "header": "🔵 <b>SETTINGS</b>",
        "body": (
            "<i>Tune the magic to your liking ⚙️</i>\n\n"
            "<b>Mask Mode</b> controls how Magic Cut works:\n\n"
            "◦ <b>Default:</b> white areas in your mask = <b>keep</b>\n"
            "◦ <b>Inverted:</b> black areas = <b>keep</b>\n\n"
            "<i>Flip this if your cutouts look wrong.</i>\n"
        ),
        "buttons": [
            [{"label": "◐ MASK MODE", "action": "settings_mask"}],
        ],
        "parent": "home",
    },

    "help": {
        "header": "🟠 <b>HELP — HOW IT WORKS</b>",
        "body": (
            "<i>New here? You're in the right place! 👋</i>\n\n"
            "Stix Magic is super simple:\n\n"
            "1️⃣ <b>Create a Pack</b> — give it a name, send one photo.\n"
            "   Your sticker pack is live on Telegram! 🎉\n\n"
            "2️⃣ <b>Add More Stickers</b> — pick your pack, send more images.\n\n"
            "3️⃣ <b>Magic Cut</b> — send a photo + a black‑and‑white mask\n"
            "   → get a clean cut‑out with no background.\n\n"
            "That's it! Tap below for detailed steps or quick tips.\n"
        ),
        "buttons": [
            [
                {"label": "📖 DETAILED STEPS", "action": "menu_help_detail"},
                {"label": "💡 QUICK TIPS", "nav": "tips"},
            ],
        ],
        "parent": "home",
    },

    "tips": {
        "header": "💡 <b>QUICK TIPS</b>",
        "body": (
            "<i>Get the best results every time ✨</i>\n\n"
            "◦ <b>PNG images</b> with a transparent background work best\n"
            "◦ Ideal image size: <b>512 × 512 px</b>\n"
            "◦ For Magic Cut — use a <b>plain black‑and‑white image</b> as your mask\n"
            "   (white = keep, black = remove — flip in Settings)\n"
            "◦ <b>Videos & GIFs</b> become animated video stickers automatically\n"
            "◦ Every sticker gets a ✨ emoji assigned automatically\n"
            "◦ Can't see your pack? Tap the 🔗 link to add it in Telegram\n"
        ),
        "buttons": [],
        "parent": "help",
    },
}


def build_keyboard(menu_id):
    menu = MENU_STRUCTURE.get(menu_id)
    if not menu:
        return InlineKeyboardMarkup([])

    rows = []
    for entry in menu["buttons"]:
        if entry == "spacer":
            continue

        row = []
        for btn in entry:
            if "nav" in btn:
                row.append(InlineKeyboardButton(btn["label"], callback_data=f"nav:{btn['nav']}"))
            elif "action" in btn:
                row.append(InlineKeyboardButton(btn["label"], callback_data=btn["action"]))
            elif "url" in btn:
                row.append(InlineKeyboardButton(btn["label"], url=btn["url"]))
            elif "web_app" in btn and MINIAPP_URL:
                row.append(InlineKeyboardButton(btn["label"], web_app=WebAppInfo(url=MINIAPP_URL)))
        if row:
            rows.append(row)

    nav_row = []
    if menu["parent"]:
        nav_row.append(InlineKeyboardButton("◂ BACK", callback_data=f"nav:{menu['parent']}"))
    if menu_id != "home":
        nav_row.append(InlineKeyboardButton("✦ HOME", callback_data="nav:home"))
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(rows)


def get_menu_text(menu_id):
    menu = MENU_STRUCTURE.get(menu_id)
    if not menu:
        return "Menu not found."

    text = f"{menu['header']}\n{DIV}\n\n"
    if menu.get("body"):
        text += menu["body"]

    return text
