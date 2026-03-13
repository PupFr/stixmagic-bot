# 🪄 Stix Magic Bot

> **Telegram sticker alchemy bot** — create, cut, and manage sticker packs with ease.

**Bot:** [@stixmagicbot](https://t.me/stixmagicbot) &nbsp;|&nbsp; **Website:** [stixmagic.com](https://stixmagic.com)

---

## What It Does

Stix Magic lets you build and manage Telegram sticker packs without leaving the chat. Send any image or short video and it becomes a sticker instantly. The `/magic` command uses a black-and-white mask photo to cut a subject cleanly out of any background.

All generated stickers go through a **Draft Vault** review process before they can be published — preventing clutter and garbage sticker sets.

---

## Product Architecture

### Core Design Principles

#### 1. Draft First

Generated stickers are **never published immediately**. Every result goes through the Draft Vault:

```
generated → draft → approved → published
generated → draft → rejected → trash
```

#### 2. Telegram-Native Experience

- Bot commands and inline keyboard buttons
- Animated "working…" feedback during processing
- Telegram built-in Sticker Editor for manual cutting (Phase 1)

#### 3. Creation Limits

Users have a creation quota per subscription tier:

| Plan    | Creations     | Max Drafts |
|---------|---------------|------------|
| Free    | 3 / day       | 10         |
| Premium | 50 / month    | 100        |
| Pro     | 300 / month   | Unlimited  |

#### 4. Draft Vault

All generated stickers live in the Draft Vault. Users can:
- **Approve** — ready to publish to a pack or collection
- **Retry** — re-generate with different settings *(Phase 2)*
- **Reject** — move to trash
- **Save for later** — extend expiry

Drafts automatically expire after 7 days.

#### 5. Animated Task Feedback

When the bot processes a request:

1. Bot sends `🧙 working on it…` placeholder
2. Processing occurs
3. Placeholder is deleted
4. Result is returned

#### 6. Sticker Disposal System

Draft lifecycle states:

| Status    | Meaning                           |
|-----------|-----------------------------------|
| `draft`   | Awaiting review in Draft Vault    |
| `approved`| Ready to publish                  |
| `published`| Added to a pack or collection    |
| `rejected` | Moved to trash by user           |
| `expired` | Auto-expired by cleanup worker    |

---

## Commands

| Command | Description |
|---|---|
| `/start` | Open the main menu |
| `/create` | Create a new sticker pack |
| `/addsticker` | Add a sticker to one of your packs |
| `/magic` | Cut a subject from its background using a B&W mask |
| `/mydrafts` | Browse pending drafts in the Draft Vault |
| `/myapproved` | View approved stickers ready to publish |
| `/trash` | View rejected and expired stickers |
| `/catalog` | Browse available sticker styles |
| `/plans` | View subscription plans and usage |
| `/packs` | Browse and manage your packs |
| `/sync` | Import an existing Telegram pack |
| `/settings` | Toggle mask inversion and other preferences |
| `/help` | Show help information |
| `/cancel` | Cancel the current operation |

---

## Features

### Draft Vault
- All generated stickers land in the vault first
- Approve to publish, reject to trash, or save for later
- Automatic expiry after 7 days (cleanup worker runs daily)
- View drafts by status: pending, approved, rejected/expired

### Sticker Pack Management
- Create packs with a custom name and emoji
- Add unlimited stickers to any of your packs
- Delete packs with a confirmation step
- Direct Telegram links on every pack button — tap to open in-app
- "Add Another" shortcut after adding a sticker (no re-navigation required)

### Media Handling
- **Static images** → converted to WEBP via Pillow, compressed to ≤ 64 KB
- **Videos / GIFs** → converted to VP9 WEBM via ffmpeg (≤ 256 KB, max 3 s, 512 px)
- Supports JPEG, PNG, GIF, MP4, and more

### Magic Cut (`/magic`)
- Send a **subject photo** then a **black-and-white mask** (white = keep, black = remove)
- The bot composites them and produces a clean cut-out sticker
- Step 1 / Step 2 progress indicators in the flow
- Mask inversion toggle in `/settings` (for dark-background masks)

### Style Catalog
- Browse styles for sticker generation by plan tier
- Free, Premium, and Pro style tiers
- Category-based organization

### Inline Menu System
- Color-coded button groups: 🟣 Create · 🔵 Explore · 🟠 Info
- 2-column keyboard layout with context-aware body text
- In-place message updates (no chat clutter)
- Cancel buttons throughout every conversation flow

---

## REST API

A Flask-based REST API runs alongside the bot.

**Base URL:** `https://stixmagic.com/api`
**Auth:** `X-API-Key: <your-key>` header (or `?api_key=` param)
**Docs:** `/api` — interactive dark-themed reference page

### Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | Public | Service + DB status |
| GET | `/api/stats` | Required | User and pack counts |
| GET | `/api/search` | Required | Search packs by name/title |
| GET | `/api/packs/<user_id>` | Required | List user's packs (paginated) |
| GET | `/api/packs/<user_id>/<name>` | Required | Get a single pack |
| DELETE | `/api/packs/<user_id>/<name>` | Required | Delete a pack record |
| GET | `/api/settings/<user_id>` | Required | Get user settings |
| PATCH | `/api/settings/<user_id>` | Required | Update user settings |

All responses use a consistent envelope:

```json
{ "ok": true, "data": { ... } }
{ "ok": false, "error": { "message": "...", "code": "..." } }
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Bot framework | [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21 |
| Image processing | [Pillow](https://python-pillow.org/) |
| Video processing | [ffmpeg](https://ffmpeg.org/) (libvpx-vp9) |
| Database | SQLite (via built-in `sqlite3`) |
| Web server | [Flask](https://flask.palletsprojects.com/) |
| Hosting | Replit |

---

## Project Structure

```
stixmagic-bot/
├── main.py           # Bot logic, conversation handlers, sticker processing,
│                     # draft vault, usage tracking, cleanup worker
├── menus.py          # Inline menu definitions (keyboard builder, menu tree)
├── api.py            # Flask REST API with auth, CORS, pagination
├── static/
│   ├── index.html    # Landing page (stixmagic.com)
│   ├── api.html      # Interactive API documentation
│   └── miniapp.html  # Telegram Mini App interface
├── requirements.txt  # Python dependencies
└── pyproject.toml    # Project metadata
```

---

## Database Schema

```sql
-- Existing tables
CREATE TABLE packs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name    TEXT NOT NULL,
    title   TEXT NOT NULL
);

CREATE TABLE user_settings (
    user_id       INTEGER PRIMARY KEY,
    mask_inverted INTEGER DEFAULT 0
);

-- User registry (plan management)
CREATE TABLE users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    username    TEXT,
    plan        TEXT NOT NULL DEFAULT 'free',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Creation-limit tracking (daily / monthly windows)
CREATE TABLE user_usage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    period_type    TEXT NOT NULL,        -- 'daily' or 'monthly'
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    creations_used INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Draft vault — all generated stickers live here first
CREATE TABLE sticker_drafts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    source_file_id    TEXT,
    generated_file_id TEXT,
    status            TEXT NOT NULL DEFAULT 'draft',
    style_id          INTEGER REFERENCES catalog_styles(id),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at        TEXT
);

-- Named collections of approved stickers
CREATE TABLE sticker_collections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Items within a collection (approved drafts)
CREATE TABLE collection_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id    INTEGER NOT NULL REFERENCES sticker_collections(id),
    draft_id         INTEGER NOT NULL REFERENCES sticker_drafts(id),
    telegram_file_id TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Style catalog (Phase 1 styles)
CREATE TABLE catalog_styles (
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
);
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
| `STIXMAGIC_API_KEY` | Auto-generated key for the REST API |
| `SESSION_SECRET` | Flask session secret |
| `MINIAPP_URL` | Mini App URL (auto-resolved from Replit domains if unset) |
| `PORT` | Web server port (default: 5000) |

See `.env.example` for a template.

---

## License

MIT

  