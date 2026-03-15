import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

DIVIDER = "◈ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ◈"

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
        "header": "⚗️ <b>STIX MAGIC</b>",
        "body": (
            "<i>the sticker alchemy laboratory</i>\n\n"
            "Every image is raw material.\n"
            "We transmute it into sticker gold.\n\n"
            "⚗️ <b>forge</b>  ·  ◈ <b>ritual</b>  ·  📖 <b>grimoire</b>  ·  🔍 <b>catalog</b>\n"
        ),
        "buttons": [
            [
                {"label": "⚗️ FORGE PACK", "action": "menu_create"},
                {"label": "◈ RITUAL CUT", "action": "menu_magic"},
            ],
            [
                {"label": "📖 GRIMOIRE ▸", "nav": "my_packs"},
                {"label": "⚙ ORACLE ▸", "nav": "settings"},
            ],
            [
                {"label": "🔍 CATALOG ▸", "nav": "catalog"},
                {"label": "📜 CODEX", "nav": "help"},
            ],
            [
                {"label": "✦ LORE", "action": "menu_about"},
                {"label": "🔮 OPEN THE PORTAL", "web_app": True},
            ],
        ],
        "parent": None,
    },

    "my_packs": {
        "header": "📖 <b>THE GRIMOIRE</b>",
        "body": (
            "<i>your bound sticker vessels</i>\n\n"
            "Inspect, inscribe, or manage\n"
            "your sticker collections.\n"
        ),
        "buttons": [
            [
                {"label": "👁 INSPECT", "action": "menu_packs"},
                {"label": "✦ INSCRIBE", "action": "menu_addsticker"},
            ],
            [
                {"label": "⚗️ CRUCIBLE", "action": "menu_manage"},
                {"label": "🔄 SUMMON PACK", "action": "menu_sync"},
            ],
        ],
        "parent": "home",
    },

    "settings": {
        "header": "⚙ <b>THE ORACLE</b>",
        "body": (
            "<i>configure the ritual parameters</i>\n\n"
            "Adjust how masks and cuts\n"
            "behave during the cutting ritual.\n"
        ),
        "buttons": [
            [{"label": "◐ ORACLE MODE", "action": "settings_mask"}],
        ],
        "parent": "home",
    },

    "help": {
        "header": "📜 <b>THE CODEX</b>",
        "body": (
            "<i>ancient knowledge of the craft</i>\n\n"
            "All rituals, all secrets —\n"
            "the complete alchemist's manual.\n"
        ),
        "buttons": [
            [
                {"label": "📖 THE CRAFT", "action": "menu_help_detail"},
                {"label": "◦ FIELD NOTES", "nav": "tips"},
            ],
        ],
        "parent": "home",
    },

    "tips": {
        "header": "◦ <b>ALCHEMIST'S FIELD NOTES</b>",
        "body": (
            "<i>hard-won knowledge from the lab</i>\n\n"
            "◦ Use <b>PNG</b> with transparency for finest results\n"
            "◦ Ideal vessel size: <b>512 × 512</b> px\n"
            "◦ Mask: white = keep · black = dissolve\n"
            "   <i>(flip this in ⚙ Oracle settings)</i>\n"
            "◦ Videos & GIFs transmute as animated stickers\n"
            "◦ Emoji sigil is auto-assigned  ✨\n"
        ),
        "buttons": [],
        "parent": "help",
    },

    "catalog": {
        "header": "🔍 <b>STICKER CATALOG</b>",
        "body": (
            "<i>discover and share sticker packs</i>\n\n"
            "Browse popular, trending, and newly added\n"
            "sticker packs shared by the community.\n"
        ),
        "buttons": [
            [
                {"label": "🔥 POPULAR", "action": "cat_sort_popular"},
                {"label": "📈 TRENDING", "action": "cat_sort_trending"},
            ],
            [
                {"label": "🆕 NEW", "action": "cat_sort_new"},
                {"label": "🔍 SEARCH", "action": "menu_catalog_search"},
            ],
            [
                {"label": "⚗️ FEATURE MY PACK", "action": "menu_feature"},
            ],
        ],
        "parent": "home",
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
