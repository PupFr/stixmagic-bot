"""
infra/db.py – SQLite persistence layer for Stix Magic.

All raw SQL lives here so the rest of the application never touches
sqlite3 directly.  api.py has its own get_db() helper that mirrors the
same DB_FILE constant; both sides read from the same file on disk.
"""

import logging
import sqlite3

DB_FILE = "bot.db"

logger = logging.getLogger(__name__)


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS packs (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name    TEXT    NOT NULL,
            title   TEXT    NOT NULL
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
    conn.commit()
    conn.close()


# ── User Settings ─────────────────────────────────────────────

def get_mask_inverted(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT mask_inverted FROM user_settings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row[0]) if row else False


def set_mask_inverted(user_id: int, inverted: bool) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO user_settings (user_id, mask_inverted) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET mask_inverted = ?",
        (user_id, int(inverted), int(inverted)),
    )
    conn.commit()
    conn.close()


# ── Pack CRUD ─────────────────────────────────────────────────

def add_pack(user_id: int, name: str, title: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO packs (user_id, name, title) VALUES (?, ?, ?)",
        (user_id, name, title),
    )
    conn.commit()
    conn.close()


def delete_pack(user_id: int, name: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM packs WHERE user_id = ? AND name = ?", (user_id, name))
    conn.commit()
    conn.close()


def get_user_packs(user_id: int) -> list[tuple[str, str]]:
    """Return a list of (name, title) tuples for the given user."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT name, title FROM packs WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def update_pack_title(user_id: int, name: str, title: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "UPDATE packs SET title = ? WHERE user_id = ? AND name = ?",
        (title, user_id, name),
    )
    conn.commit()
    conn.close()


# ── User state helpers ────────────────────────────────────────

def is_new_user(user_id: int) -> bool:
    """Return True if the user has never created a pack or changed settings."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM packs WHERE user_id = ?", (user_id,))
    packs = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM user_settings WHERE user_id = ?", (user_id,))
    settings = c.fetchone()[0]
    conn.close()
    return packs == 0 and settings == 0
