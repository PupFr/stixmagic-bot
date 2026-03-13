# Stix Magic Bot — Copilot Instructions

## Project Overview

**Stix Magic** is a Telegram-first sticker creation and management platform.
Users interact entirely through a Telegram bot (`@stixmagicbot`) to build, cut, and manage sticker packs.
A Flask REST API and a Telegram Mini App run alongside the bot.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Bot framework | python-telegram-bot v21 (async) |
| Image processing | Pillow |
| Video processing | ffmpeg (libvpx-vp9) |
| Database | SQLite (`sqlite3`) |
| Web server | Flask |
| Hosting | Replit |

---

## Repository Structure

```
stixmagic-bot/
├── main.py       # Bot logic, conversation handlers, sticker processing
├── menus.py      # Inline menu definitions (MENU_STRUCTURE dict + keyboard builder)
├── api.py        # Flask REST API with auth, CORS, pagination
├── static/
│   ├── index.html   # Landing page
│   ├── api.html     # Interactive API docs
│   └── miniapp.html # Telegram Mini App
├── requirements.txt
└── pyproject.toml
```

---

## Core Principles

- **Bot commands** live in `main.py` as `ConversationHandler` or plain command handlers.
- **Inline menus** are declared in `MENU_STRUCTURE` inside `menus.py`; `build_keyboard()` and `get_menu_text()` render them. Add new menu pages there, not inline.
- **API endpoints** follow the `/api/<resource>` pattern and use the `ok()` / `err()` helpers from `api.py`. All authenticated routes use `@require_api_key`.
- **Database** is SQLite accessed via `get_db()` in `api.py`. Schema lives in `main.py` (`init_db()`). No ORM — use raw SQL with parameterised queries.
- **Media pipeline**: static images → Pillow → WEBP ≤ 64 KB; video/GIF → ffmpeg VP9 WEBM ≤ 256 KB, max 3 s, 512 px.
- **Environment variables**: `TELEGRAM_BOT_TOKEN`, `STIXMAGIC_API_KEY`, `SESSION_SECRET`, `MINIAPP_URL` (optional), `REPLIT_DOMAINS` (optional).

---

## Coding Conventions

- Python 3.11+, async/await throughout bot handlers.
- Keep handlers small; delegate media processing to helper functions.
- Use `ConversationHandler` states as `int` constants at the top of `main.py`.
- API responses always use the `{"ok": true/false, "data": ...}` envelope.
- Inline keyboard buttons use `callback_data` strings like `"menu_<action>"` or `"nav:<menu_id>"`.
- Do not commit secrets or tokens; always read from `os.environ`.

---

## Database Schema

```sql
CREATE TABLE packs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name    TEXT NOT NULL,   -- Telegram pack name (stix_<uid>_<rand>_by_<bot>)
    title   TEXT NOT NULL    -- Display title
);

CREATE TABLE user_settings (
    user_id         INTEGER PRIMARY KEY,
    mask_inverted   INTEGER DEFAULT 0
);
```

---

## PR / Issue Workflow

- Keep PRs small and focused on a single feature or fix.
- Reference the related Issue number in the PR description.
- Preferred order of changes per PR: database → service logic → bot handler → menu → API endpoint → tests.
