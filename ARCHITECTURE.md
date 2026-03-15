# STIX MAGIC – Platform Architecture

> **Status**: Living document — updated as the platform evolves.  
> **Last revised**: 2026-03

---

## Vision

STIX MAGIC is a **Sticker Alchemy Platform**. Its core concept is to
transform images into *interactive stickers* — programmable interaction
objects that can act as chat reactions, triggers, commands, and
mini-interactions inside Telegram chats.

The platform is designed around three layers:

```
┌─────────────────────────────────────────────────────────┐
│  Interaction Layer  (Telegram Bot, Mini App, Web API)   │
├─────────────────────────────────────────────────────────┤
│  Domain Layer       (Sticker generation, Pack logic)    │
├─────────────────────────────────────────────────────────┤
│  Infrastructure     (SQLite, ffmpeg, Pillow, Flask)     │
└─────────────────────────────────────────────────────────┘
```

---

## Current Repository Layout

```
stixmagic-bot/
├── main.py          Bot logic, conversation handlers, command routing
├── menus.py         Data-driven inline keyboard registry (MENU_STRUCTURE)
├── api.py           Flask REST API + Mini App endpoints
├── domain/
│   └── media.py     Sticker generation pipeline (image, video, mask)
├── infra/
│   └── db.py        SQLite persistence layer, event log
├── static/
│   ├── index.html   Landing page
│   ├── api.html     Interactive API docs
│   └── miniapp.html Telegram Mini App
├── Dockerfile       Container build definition
├── .env.example     Environment variable template
└── requirements.txt Python dependencies
```

---

## Architecture Decisions

### Bot Framework

| Area | Current | Rationale |
|------|---------|-----------|
| Framework | python-telegram-bot v21 (async) | Well-maintained, fully async, strong typing |
| Conversation state | `ConversationHandler` | Built-in multi-step dialog handling |
| Menu system | Data-driven `MENU_STRUCTURE` dict | Declarative; add a menu page without writing handler code |
| Callback routing | Dispatch table in `menu_callback` | Extensible; O(1) lookup instead of if/elif chain |

### Async Safety

All CPU-bound and blocking I/O operations (Pillow processing, ffmpeg
subprocesses) are executed via `asyncio.run_in_executor` so they never
block the asyncio event loop. This keeps the bot responsive even while
converting large videos for multiple users simultaneously.

```
Bot handler (async)
  └─► run_in_executor(None, convert_video_to_sticker, ...)
          └─► ThreadPoolExecutor worker
                  └─► subprocess.run("ffmpeg ...")
```

### Media Pipeline

```
User sends media
  │
  ├─ image/photo ──► convert_to_sticker (Pillow)
  │                    RGBA → resize 512px → WEBP ≤ 64 KB
  │
  ├─ video/GIF  ──► convert_video_to_sticker (ffmpeg VP9)
  │                    scale 512px, fps=30, t≤3s → WEBM ≤ 256 KB
  │
  └─ image + B&W mask ──► apply_mask_to_image (Pillow)
                            alpha compositing → WEBP ≤ 64 KB
```

All pipeline functions are synchronous and pure (bytes-in / bytes-out)
for easy unit testing. Async wrappers (`async_*`) handle executor
offloading.

### REST API

```
GET  /                        Landing page
GET  /api/health              Health check (public, rate-limited 60/min)
GET  /api/miniapp/packs       Mini App: user packs (30/min)
GET  /api/miniapp/settings    Mini App: user settings (30/min)
PATCH /api/miniapp/settings   Mini App: update settings (20/min)

# Authenticated (X-API-Key header)
GET  /api/stats               Platform usage stats
GET  /api/search?q=<query>    Search packs by name/title
GET  /api/packs/<user_id>     List user's packs
GET  /api/packs/<uid>/<name>  Pack detail
DELETE /api/packs/<uid>/<name> Remove pack record
GET  /api/settings/<user_id>  User settings
PATCH /api/settings/<user_id> Update user settings
GET  /api/events              Top event counts (observability)
```

All responses use the `{"ok": bool, "data": ...}` envelope.  
Every response carries `X-Request-ID` and `X-API-Version` headers.

### Rate Limiting

A lightweight in-memory sliding-window rate limiter is applied per
client IP. No external dependency is required. Limits reset on process
restart (stateless by design; sufficient for Replit/single-instance).

| Endpoint group | Limit |
|----------------|-------|
| Public health / mini-app | 30–60 req/min |
| Authenticated API | 20–30 req/min |

### Data Layer

SQLite with WAL journal mode for improved concurrent read/write
performance. All connections use a context manager (`db_conn()`) that
guarantees the connection is closed even if an exception occurs.

```
packs          (id, user_id, name, title, created_at)
user_settings  (user_id PK, mask_inverted)
event_log      (id, user_id, event, detail, created_at)
```

`event_log` captures key bot events (`pack_created`, `sticker_added`)
and surfaces them through the `/api/events` endpoint for lightweight
analytics without an external analytics service.

### Observability

- Structured request logging (`METHOD PATH STATUS_CODE ms req=ID`)
- `X-Request-ID` response header for tracing individual requests
- Global bot error handler logs all unhandled exceptions with full stack trace
- `/api/events` endpoint exposes event frequency counts
- `/status` bot command shows live platform metrics

### Deployment

The platform ships as a single Docker image. `ffmpeg` is bundled in the
base layer. The bot polling loop and the Flask API run concurrently — the
API in a daemon thread, the bot on the main asyncio event loop.

```
docker build -t stixmagic-bot .
docker run -e TELEGRAM_BOT_TOKEN=... -e STIXMAGIC_API_KEY=... -p 5000:5000 stixmagic-bot
```

---

## Improvement Areas (Architect Review)

### Bot Architecture

#### Current Approach
Single `main.py` with all handlers. Callback routing uses a dispatch table.

#### Recommended Pattern
As the feature set grows, extract handlers into sub-modules
(`handlers/create.py`, `handlers/magic.py`, etc.) imported by a thin
`main.py` registry. This keeps each feature independently testable.

#### Priority
Adopt Later — low coupling currently; refactor when > 5 command flows.

---

### Job Processing

#### Current Approach
Video processing runs in a thread-pool executor, which is appropriate for
a single-instance deployment.

#### Recommended Pattern (future scale)
For multi-instance deployments, introduce a task queue (e.g. Celery +
Redis, or a simple PostgreSQL-backed queue) so video jobs can be
distributed across worker processes without duplicating work.

#### Priority
Adopt Later — only required when horizontal scaling is needed.

---

### File Storage

#### Current Approach
Media is processed in-memory and handed directly to Telegram. No
persistent file storage.

#### Recommended Pattern (future)
For sticker re-use, templating, and analytics, persist generated assets
in object storage (S3-compatible or Cloudflare R2). Store asset URLs in
the DB.

#### Priority
Optional — relevant if sticker templating or re-use features are added.

---

### Data Layer

#### Current Approach
SQLite with WAL mode. Sufficient for single-instance Replit deployment.

#### Recommended Pattern (future scale)
Migrate to PostgreSQL when multi-instance deployment is required. The
abstraction in `infra/db.py` means only that module needs to change.

#### Priority
Adopt Later — migrate when horizontal scaling or high write concurrency
is needed.

---

### Security

#### Current Approach
- API key authentication for admin endpoints
- In-memory rate limiting per IP
- Parameterised SQL queries throughout

#### Known Gap
Mini App endpoints (`/api/miniapp/*`) accept a `user_id` query parameter
without Telegram initData HMAC validation. A malicious caller who knows
another user's ID can read their pack list.

#### Recommended Fix
Validate the `initData` string from `window.Telegram.WebApp.initData`
using the bot token as the HMAC key (documented in Telegram Mini App
docs). Pass the raw `initData` as a header instead of a plain `user_id`.

#### Priority
**Adopt Now** — should be addressed before the Mini App goes public.

---

### Innovative Features (Roadmap)

The following capabilities would make STIX MAGIC unique in the sticker
ecosystem:

| Feature | Description |
|---------|-------------|
| **Sticker Triggers** | Assign a bot action (poll, link, reply) to a sticker; bot fires the action when users react with it |
| **Reaction Automation** | Watch for sticker reactions in groups and trigger workflows |
| **Programmable Stickers** | Attach metadata to stickers (labels, commands, URLs) stored server-side |
| **Sticker Analytics** | Track how often each sticker is used via reaction webhooks |
| **Pack Templates** | Pre-designed pack layouts users can fill with their own images |
| **Community Packs** | Shared public pack discovery and voting |

---

## Guiding Principles

- **Async-first** — blocking work runs in thread executors, never on the event loop
- **Dependency minimalism** — prefer stdlib or well-established packages; avoid framework bloat
- **Modular boundaries** — `domain/` has no knowledge of Telegram or Flask; `infra/` has no business logic
- **Parameterised SQL** — never interpolate user input into queries
- **Observable by default** — every request is logged and carries a trace ID
