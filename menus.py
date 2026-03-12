import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

DIVIDER = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─"

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
        "header": "✦ <b>STIX MAGIC</b>",
        "body": (
            "<i>sticker alchemy at your fingertips</i>\n\n"
            "Send any image. We turn it into\n"
            "a sticker — clean, cut, packed.\n\n"
            "🟣 <b>create</b>  ·  🔵 <b>explore</b>  ·  🟠 <b>more</b>\n"
        ),
        "buttons": [
            [
                {"label": "🟣 CREATE PACK", "action": "menu_create"},
                {"label": "🟣 MAGIC CUT", "action": "menu_magic"},
            ],
            [
                {"label": "🔵 MY PACKS ▸", "nav": "my_packs"},
                {"label": "🔵 SETTINGS ▸", "nav": "settings"},
            ],
            [
                {"label": "🟠 HELP", "nav": "help"},
                {"label": "🟠 ABOUT", "action": "menu_about"},
            ],
            [
                {"label": "🖥 OPEN MINI APP", "web_app": True},
            ],
        ],
        "parent": None,
    },

    "my_packs": {
        "header": "🔵 <b>MY PACKS</b>",
        "body": (
            "<i>your sticker collection lives here</i>\n\n"
            "View, add to, or manage your\n"
            "existing sticker packs.\n"
        ),
        "buttons": [
            [
                {"label": "👁‍🗨 VIEW", "action": "menu_packs"},
                {"label": "＋ ADD", "action": "menu_addsticker"},
            ],
            [{"label": "⚡ MANAGE", "action": "menu_manage"}],
        ],
        "parent": "home",
    },

    "settings": {
        "header": "🔵 <b>SETTINGS</b>",
        "body": (
            "<i>tune the wizard to your liking</i>\n\n"
            "Configure how masks and cuts\n"
            "behave during magic operations.\n"
        ),
        "buttons": [
            [{"label": "◐ MASK MODE", "action": "settings_mask"}],
        ],
        "parent": "home",
    },

    "help": {
        "header": "🟠 <b>HELP</b>",
        "body": (
            "<i>learn the craft</i>\n\n"
            "Everything you need to know\n"
            "about creating sticker magic.\n"
        ),
        "buttons": [
            [
                {"label": "▸ HOW IT WORKS", "action": "menu_help_detail"},
                {"label": "△ TIPS", "nav": "tips"},
            ],
        ],
        "parent": "home",
    },

    "tips": {
        "header": "🟠 <b>TIPS & TRICKS</b>",
        "body": (
            "<i>get the most out of every cut</i>\n\n"
            "◦ Use <b>PNG</b> with transparency for best results\n"
            "◦ Ideal size: <b>512 × 512</b> px\n"
            "◦ Mask: white = keep · black = remove\n"
            "   <i>(flip this in ⚙ Settings)</i>\n"
            "◦ Videos & GIFs work as video stickers\n"
            "◦ Emoji is auto-assigned  ✨\n"
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

    text = f"{menu['header']}\n{DIVIDER}\n\n"
    if menu.get("body"):
        text += menu["body"]

    return text
