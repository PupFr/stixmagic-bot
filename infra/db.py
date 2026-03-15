"""
infra/db.py – SQLite persistence layer for Stix Magic.

All raw SQL lives here so the rest of the application never touches
sqlite3 directly.  api.py has its own get_db() helper that mirrors the
same DB_FILE constant; both sides read from the same file on disk.
"""

import logging
import sqlite3
from contextlib import contextmanager

DB_FILE = "bot.db"

logger = logging.getLogger(__name__)


@contextmanager
def db_conn():
    """Context manager that yields a connected sqlite3 connection and
    guarantees it is closed on exit, even if an exception is raised."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrent read/write performance
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and add any missing columns if they don't exist."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS packs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT    NOT NULL,
                title      TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id       INTEGER PRIMARY KEY,
                mask_inverted INTEGER DEFAULT 0
            )
            """
        )
        # Add created_at to existing packs table if it was created before this migration
        try:
            c.execute("ALTER TABLE packs ADD COLUMN created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Event log for observability / analytics
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS event_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                event      TEXT    NOT NULL,
                detail     TEXT,
                created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
            """
        )
        conn.commit()


# ── User Settings ─────────────────────────────────────────────

def get_mask_inverted(user_id: int) -> bool:
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT mask_inverted FROM user_settings WHERE user_id = ?", (user_id,))
        row = c.fetchone()
    return bool(row[0]) if row else False


def set_mask_inverted(user_id: int, inverted: bool) -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, mask_inverted) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET mask_inverted = ?",
            (user_id, int(inverted), int(inverted)),
        )
        conn.commit()


# ── Pack CRUD ─────────────────────────────────────────────────

def add_pack(user_id: int, name: str, title: str) -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO packs (user_id, name, title) VALUES (?, ?, ?)",
            (user_id, name, title),
        )
        conn.commit()


def delete_pack(user_id: int, name: str) -> None:
    with db_conn() as conn:
        conn.execute("DELETE FROM packs WHERE user_id = ? AND name = ?", (user_id, name))
        conn.commit()


def get_user_packs(user_id: int) -> list[tuple[str, str]]:
    """Return a list of (name, title) tuples for the given user."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT name, title FROM packs WHERE user_id = ?", (user_id,))
        return [(row["name"], row["title"]) for row in c.fetchall()]


def update_pack_title(user_id: int, name: str, title: str) -> None:
    with db_conn() as conn:
        conn.execute(
            "UPDATE packs SET title = ? WHERE user_id = ? AND name = ?",
            (title, user_id, name),
        )
        conn.commit()


# ── User state helpers ────────────────────────────────────────

def is_new_user(user_id: int) -> bool:
    """Return True if the user has never created a pack or changed settings."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM packs WHERE user_id = ?", (user_id,))
        packs = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM user_settings WHERE user_id = ?", (user_id,))
        settings = c.fetchone()[0]
    return packs == 0 and settings == 0


# ── Event log ─────────────────────────────────────────────────

def log_event(user_id: int | None, event: str, detail: str | None = None) -> None:
    """Record a named event for observability/analytics (best-effort, never raises)."""
    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO event_log (user_id, event, detail) VALUES (?, ?, ?)",
                (user_id, event, detail),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("event_log write failed: %s", exc)


def get_event_counts(limit: int = 20) -> list[dict]:
    """Return top events by frequency (for admin stats endpoint)."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT event, COUNT(*) AS cnt FROM event_log "
            "GROUP BY event ORDER BY cnt DESC LIMIT ?",
            (limit,),
        )
        return [{"event": r["event"], "count": r["cnt"]} for r in c.fetchall()]
