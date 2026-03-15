# Stix Magic — Branch Review & Integration Plan

> **Purpose:** This document reviews every agent-authored branch in this repository, identifies the most valuable sticker-related functionality, and provides a prioritised plan for integrating those features into `main`.  No production code is changed here.

---

## 1. Already-Merged Baseline

Three PRs have been merged into `main` and form the current foundation:

| PR | Branch | What landed |
|---|---|---|
| #5 | `copilot/update-mini-app-alchemy-visuals` | Alchemist/magic brand theme — landing page SVG wand animation, STIX MAGIC gradient title, Mini App palette aligned to `#080c18` / indigo / cyan |
| #8 | `copilot/refactor-stix-magic-workflow` | Rebrand prompt + Phase 1–3 intent locked in |
| #9 | `copilot/stix-magic-adaptation` | **Current `main`** — monolith split into `infra/db.py` (database) + `domain/media.py` (media processing); `menus.py` menu system extracted; branding updated |

`main` today is a clean modular mono-repo: `main.py` → orchestration, `infra/db.py` → SQLite, `domain/media.py` → Pillow/ffmpeg, `menus.py` → inline keyboards.  It is the correct base for all remaining branches.

---

## 2. Branch-by-Branch Review

### PR #1 — `copilot/update-addsticker-command`
**Title:** Security: escape user-controlled strings in HTML messages; bump Pillow to 12.1.1

| Attribute | Detail |
|---|---|
| Files changed | `main.py` (+14 / -12), `requirements.txt` |
| Status | Open draft — has merge conflicts |
| Sticker-related scope | `/addsticker` UX + all HTML-mode sticker messages |

**Key features:**
- `html.escape()` applied to all user-supplied pack titles and exception messages interpolated into `parse_mode="HTML"` Telegram messages (11 call-sites) — closes an HTML-injection vector.
- `/addsticker` pack selection converted from free-text input to an inline `InlineKeyboardButton` grid — no more mistyped pack names.
- `/help` command added (mirrors `/start`).
- Pillow bumped `10.4.0 → 12.1.1`, patching a PSD out-of-bounds write CVE.

**Assessment:** 🔴 **Critical / Highest priority.**  The HTML-injection fix and CVE patch are non-negotiable security items.  The UX improvement to `/addsticker` is also high-value and low-risk.

---

### PR #3 — `copilot/adopt-premium-functionality`
**Title:** Add OpenAI DALL-E integration and self-service premium subscription with Telegram Stars

| Attribute | Detail |
|---|---|
| Files changed | `main.py`, `infra/db.py`, `menus.py`, `openai_helper.py` (new), `requirements.txt` |
| Additions | +526 lines |
| Status | Open draft — has merge conflicts |

**Key features:**

*Premium subscription (Telegram Stars — no external payment processor):*

| Plan | Price | Duration |
|---|---|---|
| 1 Month | 250 ⭐ | 30 days |
| 3 Months | 599 ⭐ (−20 %) | 90 days |
| Lifetime | 999 ⭐ | Forever |

- `premium_users` table with `expires_at` — `NULL` = lifetime (admin-granted).
- `is_premium_user()` checks expiry on every guarded call.
- `grant_premium(user_id, days=None)` stacks renewals correctly.
- `/grantpremium` and `/revokepremium` admin commands gated by `ADMIN_USER_ID`.
- Full `PreCheckoutQueryHandler` + `SUCCESSFUL_PAYMENT` handler auto-grants access.
- `⭐ PREMIUM ▸` and `⭐ AI GENERATE` discovery buttons visible to all users on the home menu; free-tier users are redirected to the pricing page, never a dead-end error.

*AI sticker generation (`/generate`):*
- `openai_helper.py` — lazy-init `OpenAI` client from `OPENAI_API_KEY`; separates API vs download error paths.
- DALL-E 3 generates image → converts to WEBP sticker → user can add to a pack or download.
- **Zero OpenAI API calls for non-premium users** — the guard returns `ConversationHandler.END` immediately.

*New env vars required:* `OPENAI_API_KEY`, `ADMIN_USER_ID`.

**Assessment:** 🟠 **Very high value — innovative monetisation + AI generation.**  Needs conflict resolution relative to the current `main`.  The zero-query guarantee for non-premium users is a good design choice that must be preserved.

---

### PR #4 — `copilot/improve-telegram-miniapp-design`
**Title:** Redesign Telegram Mini App UI — dark/light themes, icons, animations, responsive polish

| Attribute | Detail |
|---|---|
| Files changed | `static/miniapp.html` (new, +1308), `api.py` (+minor routes) |
| Status | Open draft — has merge conflicts |

**Key features:**
- **Dark/light mode toggle** — persists via `localStorage`; auto-initialises from `tg.colorScheme` / `prefers-color-scheme`.
- **Font Awesome 6** icons on all labels, tabs, buttons, pack-card actions.
- **Inter** font via Google Fonts.
- Glassmorphic sticky header (`backdrop-filter: blur`) + animated `✦` logo glow pulse.
- Tab switch animations (direction-aware left/right slide).
- Pack cards stagger in on render; hover lifts with shadow.
- Character counters (amber → red near limit) on pack name/title inputs.
- `[data-tooltip]` CSS tooltips on icon buttons and CTA buttons.
- **Accessibility:** `role="tablist"`, `role="tabpanel"`, `aria-selected` on all tabs; `aria-label` on icon buttons.
- Responsive: tighter padding at ≤ 360 px; centred max-width at ≥ 600 px.
- **Security fix:** `postMessage` origin check tightened to the three explicit Telegram Web App hostnames; same-origin messages rejected.
- **Bug fix:** Removed broken `renderPacksLoading('add-packs-list')` / `renderEmptyPacks('add-packs-list')` calls (container ID never existed).
- `/miniapp` and `/miniapp/` routes added to `api.py`.

**Assessment:** 🟡 **High UX/polish value.**  The dark/light theme and accessibility work are important long-term.  The `postMessage` security fix is notable.  Conflict resolution needed.

---

### PR #6 — `copilot/architecture-review-alignment`
**Title:** Align repository with Stix Magic product architecture: draft vault, usage tracking, catalog, cleanup worker

| Attribute | Detail |
|---|---|
| Files changed | `main.py`, `infra/db.py`, `menus.py`, `README.md` |
| Additions | +935 lines |
| Status | Open draft — has merge conflicts |

**Key features:**

*Six new database tables:*

| Table | Purpose |
|---|---|
| `users` | User registry + plan tier (`free` / `premium` / `pro`) |
| `user_usage` | Creation quota per billing window (daily/monthly) |
| `sticker_drafts` | Draft vault — pending review stickers |
| `sticker_collections` | Named collections of approved stickers |
| `collection_items` | Approved draft → collection membership |
| `catalog_styles` | Style catalog with per-tier access control |

*Draft lifecycle:* `generated → draft → approved → published` / `rejected → trash → expired`

*Creation limits:*

| Plan | Limit |
|---|---|
| Free | 3 creations / day, 10 drafts |
| Premium | 50 creations / month, 100 drafts |
| Pro | 300 creations / month, unlimited drafts |

- `check_creation_limit()` / `increment_usage()` gate usage against `user_usage`.
- `DRAFT_EXPIRY_DAYS = 7`; `cleanup_worker()` daemon thread runs daily.
- New bot commands: `/mydrafts`, `/myapproved`, `/trash`, `/catalog`, `/plans`.
- `/mydrafts` surfaces four inline actions: **Approve / Reject / Retry / Save for later**.
- `send_working_animation()` / `delete_working_animation()` helpers for Telegram-native processing feedback.
- Home menu extended with Draft Vault, Catalog, Plans entries.
- `README.md` rewritten with architecture overview, lifecycle diagram, and schema.

**Assessment:** 🟠 **Very high value — the core product architecture.**  This is the "draft-first" model that defines Stix Magic.  It must be merged (and conflicts resolved) before PR#10 or PR#3.

---

### PR #10 — `copilot/integrate-fstik-app-features`
**Title:** feat: integrate fstik-app catalog — sticker discovery, inline search, ratings & Mini App tab

| Attribute | Detail |
|---|---|
| Files changed | `api.py` (+238), `infra/db.py` (+233), `main.py` (+525), `menus.py` (+26), `static/index.html` (+7), `static/miniapp.html` (+200) |
| Status | Open draft — **mergeable (clean, no conflicts)** |

**Key features:**

*Bot commands / handlers:*
- `/catalog` — paginated browse with Popular / Trending / New sort tabs.
- `/search <query>` — in-bot catalog search.
- `/feature` — publish one of the user's packs to the community catalog.
- `/info <pack_name>` — per-pack stats (views, likes, dislikes, type).
- `InlineQueryHandler` — `@botname <query>` returns catalog packs in any chat (inline bot mode).
- Callback handlers for pagination (`cat_page_*`), sort switching (`cat_sort_*`), and reactions (`cat_like_*` / `cat_dislike_*`) including toggle-off.
- All commands registered with Telegram via `set_my_commands()` in `post_init`.

*Database tables:*
- `catalog_packs` — name, title, description, type, public, safe, likes, dislikes, view_count, added_at, added_by.
- `catalog_reactions` — composite PK `(user_id, pack_name)`, reaction enum `like|dislike`.

*REST API endpoints (fstik-app compatible schema):*
- `GET /api/catalog/packs?type=popular|trending|new|search&q=…&limit=&skip=`
- `GET /api/catalog/packs/<name>` — single pack + view increment
- `POST /api/catalog/packs/<name>/react` — like/dislike with toggle
- `POST /api/catalog/packs/<name>/feature` — feature from Mini App

*Mini App:* 4th **🔍 Catalog** tab added — browse Popular/Trending/New, debounced inline search, 👍👎 reaction buttons, ➕ Add-to-Telegram links, Feature My Pack button; tab grid updated 3 → 4 columns; `aria-label` on all action buttons.

*Landing page:* Added Sticker Catalog feature card.

**Assessment:** 🟢 **High value, clean merge.**  Community catalog is a major differentiator.  This is the only large PR with zero merge conflicts relative to current `main`.  It is the best candidate for the first functional merge after security fixes.

---

### PR #12 — `copilot/improve-platform-architecture`
**Title:** [WIP] Refactor architecture for STIX MAGIC platform

| Attribute | Detail |
|---|---|
| Files changed | `.env.example` (new), `ARCHITECTURE.md` (new), `Dockerfile` (new), `api.py` (+90), `domain/media.py` (+48), `infra/db.py` (+122), `main.py` (+58) |
| Status | Open draft — **mergeable (clean, no conflicts)** |

**Key features:**

*Async safety (`domain/media.py`):*
- `async_convert_to_sticker`, `async_convert_video_to_sticker`, `async_apply_mask_to_image` — blocking PIL/ffmpeg calls moved to `loop.run_in_executor(None, ...)`.
- `finally` block guarantees temp-file cleanup in video pipeline.

*Observability & operations:*
- Global error handler for uncaught bot exceptions.
- `/status` command — live user/pack/event counts in chat.
- `log_event()` + `get_event_counts()` for lightweight analytics.
- `event_log` table: type, user_id, metadata, ts.
- `pack_created` and `sticker_added` events instrumented.
- `/api/events` endpoint returns top event counts.

*DB layer (`infra/db.py`):*
- `db_conn()` context manager — guaranteed connection close on exception.
- WAL mode enabled (`PRAGMA journal_mode=WAL`) for concurrent reads.
- `created_at` timestamps on `packs` and `user_settings`.

*API layer (`api.py`):*
- In-memory sliding-window rate limiter (no new dependencies).
- Request-ID middleware + structured request logging.
- Configurable CORS via `CORS_ALLOW_ORIGIN` env var.

*Infrastructure:*
- `Dockerfile` for containerisation.
- `.env.example` configuration template.
- `ARCHITECTURE.md` — full platform architecture document.

*Callback routing (`main.py`):*
- Dispatch table replaces chained `if/elif` — O(1) lookup, easily extensible.

**Assessment:** 🟢 **High value, clean merge, production-ready improvements.**  Async safety is critical for avoiding bot freezes under load.  WAL mode prevents DB contention.  Clean merge makes this a strong candidate to go in alongside or just after PR#10.

---

## 3. Innovation Highlights

| Feature | Branch | Why it stands out |
|---|---|---|
| **AI sticker generation** (DALL-E 3) | PR#3 | First-class premium feature; prompt → sticker pipeline with zero API waste for free users |
| **Draft-first lifecycle** | PR#6 | Prevents sticker pack clutter — the key product differentiator over plain pack managers |
| **Community catalog** with inline bot mode | PR#10 | Turns Stix Magic into a sticker discovery platform; inline mode works in any chat |
| **Telegram Stars subscription** | PR#3 | Native payment, no external processor; tiered pricing with stacking renewals |
| **Async media processing** | PR#12 | Prevents bot event-loop blocking during ffmpeg/Pillow — critical for reliability |
| **Dark/light theme + animations** | PR#4 | Matches Telegram's own UX conventions; persisted preference via `localStorage` |
| **Usage quotas + plan tiers** | PR#6 | Monetisation infrastructure: free/premium/pro with per-period limits |
| **`postMessage` origin hardening** | PR#4 | Closes a XSS-adjacent Mini App vector |
| **HTML injection fix** | PR#1 | Closes a real injection vector in live bot messages |

---

## 4. Integration Plan

### Guiding principles
1. **Security before features** — merge the HTML-injection and CVE fixes first.
2. **Infrastructure before product** — merge async safety, WAL mode, and architecture scaffolding before business-logic features.
3. **Schema migrations before consumers** — draft-vault schema must land before premium/catalog code that touches those tables.
4. **Largest clean PRs first** — PRs with no conflicts can be merged as-is; conflicted PRs need rebase onto the updated `main` after each wave.

---

### Recommended merge order

#### Wave 1 — Security & Foundations (merge now)

| # | PR | Why first |
|---|---|---|
| 1 | **PR #1** — Security: HTML escape + Pillow 12.1.1 | Patching an active injection vector and a CVE; smallest changeset; resolve conflicts manually |
| 2 | **PR #12** — Platform architecture | Clean merge; async safety prevents event-loop freezes; WAL mode; Dockerfile; no business-logic conflict |

*After Wave 1:* `main` is secure, async-safe, observable, and containerisable.

---

#### Wave 2 — Core Product Architecture (rebase + merge)

| # | PR | Why second |
|---|---|---|
| 3 | **PR #6** — Draft vault + usage tracking | Defines the core Stix Magic schema; all later features depend on `sticker_drafts`, `user_usage`, `catalog_styles`; requires conflict resolution against Wave 1 changes |

*After Wave 2:* `main` has the draft-first lifecycle, creation quotas, cleanup worker, and command scaffolding.

---

#### Wave 3 — Community & Discovery (merge)

| # | PR | Why third |
|---|---|---|
| 4 | **PR #10** — fstik catalog + inline mode | Currently clean, no conflicts; adds `/catalog`, `/search`, inline bot mode, reactions, 4-tab Mini App; can be applied directly after Wave 2 schema is in place (the `catalog_packs` table in PR#10 is independent from PR#6's `catalog_styles` — both can coexist) |

*After Wave 3:* `main` supports community sticker discovery, inline search in any chat, and pack reactions.

---

#### Wave 4 — Premium & AI Generation (rebase + merge)

| # | PR | Why fourth |
|---|---|---|
| 5 | **PR #3** — OpenAI DALL-E + Telegram Stars | Needs `premium_users` schema + `is_premium_user()` from the base; requires rebase onto Wave 1–3 `main`; introduces `openai_helper.py` and two new env vars (`OPENAI_API_KEY`, `ADMIN_USER_ID`) |

*After Wave 4:* `main` supports self-service premium subscription and AI sticker generation.

---

#### Wave 5 — Mini App UI Polish (rebase + merge)

| # | PR | Why last |
|---|---|---|
| 6 | **PR #4** — Mini App redesign | Largest visual changeset; depends on Mini App tab layout from PR#10 (4 tabs) being stable; requires rebase; dark/light mode, animations, accessibility, origin hardening |

*After Wave 5:* `main` has a production-quality Mini App.

---

### Conflict resolution notes

| PR | Likely conflict area | Recommended approach |
|---|---|---|
| PR #1 | `main.py` HTML escape vs. modular restructure | Reapply 11 `html.escape()` call-sites to current `main.py`; cherry-pick Pillow bump |
| PR #6 | `infra/db.py` and `main.py` (both touched by PR#12) | Rebase PR#6 onto Wave-1 `main`; keep PR#12's `db_conn()` context manager and WAL pragma; merge schema additions |
| PR #3 | `menus.py` home menu + `main.py` handlers (touched by PR#6 and PR#10) | Rebase PR#3 onto Wave-3 `main`; preserve the premium guard pattern exactly |
| PR #4 | `static/miniapp.html` (overwritten by PR#10's Catalog tab additions) | Merge PR#10 first; then apply PR#4's theme/animation/accessibility CSS/JS on top |

---

## 5. What NOT to merge

| Branch | Reason |
|---|---|
| `copilot/configure-github-settings-for-pr-workflow` (PR#7) | Already closed; PR template and Copilot instructions were adopted separately |
| `enhanced-readme` | Superseded by PR#6's README rewrite and PR#12's `ARCHITECTURE.md` |

---

## 6. New environment variables summary

After all waves are merged, the complete set of env vars required is:

| Variable | Introduced by | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Existing | Bot authentication |
| `STIXMAGIC_API_KEY` | Existing | REST API authentication |
| `SESSION_SECRET` | Existing | Flask session signing |
| `MINIAPP_URL` | Existing | Mini App URL (optional) |
| `REPLIT_DOMAINS` | Existing | CORS allow-list (Replit) |
| `OPENAI_API_KEY` | PR #3 | DALL-E 3 AI generation |
| `ADMIN_USER_ID` | PR #3 | Telegram user ID for `/grantpremium` |
| `CORS_ALLOW_ORIGIN` | PR #12 | Configurable CORS (optional) |

`.env.example` (added by PR#12) documents all of these.

---

## 7. Effort estimate

| Wave | Estimated effort | Risk |
|---|---|---|
| Wave 1 (security + infra) | 1–2 hours conflict resolution | Low |
| Wave 2 (draft vault) | 2–3 hours rebase + QA | Medium |
| Wave 3 (catalog) | 30 minutes — clean merge | Low |
| Wave 4 (premium + AI) | 2–3 hours rebase + end-to-end test of payment flow | Medium |
| Wave 5 (Mini App UI) | 1–2 hours merge + visual QA on mobile | Low–Medium |

**Total estimated integration effort: ~8–12 hours** across five sequential merge sessions.

---

*Document authored by GitHub Copilot coding agent — Branch review of `FriskyDevelopments/stixmagic-bot`, 2026-03-15.*
