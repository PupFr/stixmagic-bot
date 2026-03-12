# Stix Magic — Sticker Alchemy

## Overview
Telegram bot for creating and managing sticker packs. Built with Python, `python-telegram-bot` (v21.11.1), SQLite, and Pillow. Landing page served alongside the bot on port 5000.

## Architecture
- **main.py**: Bot handlers, DB logic, image processing, conversation flows
- **api.py**: Flask REST API + landing page server (port 5000)
- **menus.py**: Modular menu registry — data-driven inline keyboard definitions, navigation builder
- **static/index.html**: Landing page for stixmagic.com (served on port 5000)
- **bot.db**: SQLite database (runtime, stores pack records + user settings)

## Menu System (menus.py)
- **MENU_STRUCTURE**: Dict of menu definitions, each with `header`, `body`, `buttons`, and `parent`
- **Button types**: `nav` (submenu navigation), `action` (triggers bot feature), `url` (external link)
- **Navigation**: `nav:` callback prefix for pure menu navigation, `menu_` prefix for action callbacks
- **Back/Home**: Auto-added based on `parent` chain
- Menus: home, my_packs, settings, help, tips

## Web API (api.py)
All endpoints except `/` and `/api/health` require API key auth via `X-API-Key` header or `api_key` query param.
- `GET /` — Landing page
- `GET /api/health` — Health check (public)
- `GET /api/stats` — Total users, packs, settings users count
- `GET /api/search?q=<query>` — Search packs by name or title
- `GET /api/packs/<user_id>` — List packs for a user
- `GET /api/packs/<user_id>/<pack_name>` — Pack detail with Telegram link
- `DELETE /api/packs/<user_id>/<pack_name>` — Delete a pack record
- `GET /api/settings/<user_id>` — User settings (mask mode)
- `PATCH /api/settings/<user_id>` — Update user settings (JSON body)

## Key Dependencies
- `python-telegram-bot` (v21.11.1)
- `Flask` (web API)
- `Pillow` (image processing for mask-based sticker cutting)
- SQLite (built-in)

## Environment Variables
- `TELEGRAM_BOT_TOKEN`: Bot token (supports raw token or full BotFather message)
- `STIXMAGIC_API_KEY`: API key for authenticating web API requests

## DB Tables
- **packs**: id, user_id, name, title
- **user_settings**: user_id (PK), mask_inverted (0/1, default 0)

## Bot Features
1. `/start` — Main menu with inline keyboard
2. `/create` — Create new sticker pack (name → sticker)
3. `/magic` — Magic tools: mask-based sticker cutting (source + B&W mask → sticker)
4. `/packs` — View your packs with links
5. `/manage` — Manage existing packs
6. `/help` — How it works
7. `/about` — About Stix Magic
8. `/cancel` — Cancel any operation
9. Settings > Mask Mode — Toggle white=keep vs black=keep

## Handler Registration Order
1. Command handlers (start, packs, manage, help, about)
2. Conversation handlers (create, addsticker, magic) — with CallbackQuery entry points
3. `nav:` CallbackQueryHandler (pure menu navigation)
4. Fallback CallbackQueryHandler (action callbacks)

## Conversation States
- `WAITING_TITLE=0, WAITING_STICKER=1` (create flow)
- `CHOOSING_PACK=2, WAITING_STICKER_ADD=3` (addsticker flow)
- `WAITING_SOURCE_IMAGE=4, WAITING_MASK_IMAGE=5, WAITING_CUT_PACK=6` (magic flow)

## Technical Notes
- `InputSticker` requires `format="static"` in constructor (v21.11.1)
- Pack name prefix: `stix_` + user_id + random suffix + `_by_` + bot username
- Default sticker emoji: ✨
- Landing page web server runs in a daemon thread alongside the bot
- Domain: stixmagic.com

## Branding
- Brand name: **Stix Magic**
- Tagline: **Sticker Alchemy**
- Tone: sleek, magical, premium
- Symbol: ✦
