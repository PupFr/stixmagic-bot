"""
infra/db.py – SQLite persistence layer for Stix Magic.

All raw SQL lives here so the rest of the application never touches
sqlite3 directly.  api.py has its own get_db() helper that mirrors the
same DB_FILE constant; both sides read from the same file on disk.
"""

import logging
import sqlite3
import time

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
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_packs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    UNIQUE NOT NULL,
            title       TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            type        TEXT    DEFAULT 'image',
            public      INTEGER DEFAULT 1,
            safe        INTEGER DEFAULT 1,
            likes       INTEGER DEFAULT 0,
            dislikes    INTEGER DEFAULT 0,
            view_count  INTEGER DEFAULT 0,
            added_at    INTEGER NOT NULL,
            added_by    INTEGER
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_reactions (
            user_id   INTEGER NOT NULL,
            pack_name TEXT    NOT NULL,
            reaction  TEXT    NOT NULL,
            PRIMARY KEY (user_id, pack_name)
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


# ── Catalog CRUD ──────────────────────────────────────────────

def catalog_add_pack(
    name: str,
    title: str,
    added_by: int,
    description: str = "",
    pack_type: str = "image",
) -> bool:
    """Add a pack to the catalog. Returns True if inserted, False if already exists."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM catalog_packs WHERE name = ?", (name,))
    if c.fetchone():
        conn.close()
        return False
    c.execute(
        """
        INSERT INTO catalog_packs (name, title, description, type, added_at, added_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, title, description, pack_type, int(time.time()), added_by),
    )
    conn.commit()
    conn.close()
    return True


def catalog_get_pack(name: str) -> dict | None:
    """Return a catalog pack as a dict, or None."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM catalog_packs WHERE name = ? AND public = 1", (name,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def catalog_search(
    query: str = "",
    sort: str = "popular",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Search the catalog.  sort: 'popular' | 'trending' | 'new' | 'search'."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if sort == "popular":
        sql = (
            "SELECT * FROM catalog_packs WHERE public = 1 "
            "ORDER BY likes DESC LIMIT ? OFFSET ?"
        )
        c.execute(sql, (limit, offset))
    elif sort == "trending":
        sql = (
            "SELECT * FROM catalog_packs WHERE public = 1 "
            "ORDER BY view_count DESC, likes DESC LIMIT ? OFFSET ?"
        )
        c.execute(sql, (limit, offset))
    elif sort == "new":
        sql = (
            "SELECT * FROM catalog_packs WHERE public = 1 "
            "ORDER BY added_at DESC LIMIT ? OFFSET ?"
        )
        c.execute(sql, (limit, offset))
    else:
        sql = (
            "SELECT * FROM catalog_packs WHERE public = 1 "
            "AND (title LIKE ? OR name LIKE ? OR description LIKE ?) "
            "ORDER BY likes DESC LIMIT ? OFFSET ?"
        )
        pattern = f"%{query}%"
        c.execute(sql, (pattern, pattern, pattern, limit, offset))

    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def catalog_count(query: str = "", sort: str = "popular") -> int:
    """Return total count for a catalog query."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if sort in ("popular", "trending", "new"):
        c.execute("SELECT COUNT(*) FROM catalog_packs WHERE public = 1")
    else:
        pattern = f"%{query}%"
        c.execute(
            "SELECT COUNT(*) FROM catalog_packs WHERE public = 1 "
            "AND (title LIKE ? OR name LIKE ? OR description LIKE ?)",
            (pattern, pattern, pattern),
        )
    total = c.fetchone()[0]
    conn.close()
    return total


def catalog_increment_views(name: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE catalog_packs SET view_count = view_count + 1 WHERE name = ?", (name,)
    )
    conn.commit()
    conn.close()


def catalog_react(user_id: int, pack_name: str, reaction: str) -> dict:
    """
    Toggle like/dislike.  Returns {"likes": int, "dislikes": int, "current": str|None}.
    """
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Whitelist-only column name mapping — never interpolated from user input
    _COL = {"like": "likes", "dislike": "dislikes"}

    c.execute(
        "SELECT reaction FROM catalog_reactions WHERE user_id = ? AND pack_name = ?",
        (user_id, pack_name),
    )
    existing = c.fetchone()

    if existing:
        old = existing["reaction"]
        if old == reaction:
            # Toggle off
            c.execute(
                "DELETE FROM catalog_reactions WHERE user_id = ? AND pack_name = ?",
                (user_id, pack_name),
            )
            if reaction == "like":
                c.execute(
                    "UPDATE catalog_packs SET likes = MAX(0, likes - 1) WHERE name = ?",
                    (pack_name,),
                )
            else:
                c.execute(
                    "UPDATE catalog_packs SET dislikes = MAX(0, dislikes - 1) WHERE name = ?",
                    (pack_name,),
                )
            current = None
        else:
            # Switch reaction
            c.execute(
                "UPDATE catalog_reactions SET reaction = ? WHERE user_id = ? AND pack_name = ?",
                (reaction, user_id, pack_name),
            )
            if reaction == "like":
                c.execute(
                    "UPDATE catalog_packs SET likes = likes + 1, dislikes = MAX(0, dislikes - 1) WHERE name = ?",
                    (pack_name,),
                )
            else:
                c.execute(
                    "UPDATE catalog_packs SET dislikes = dislikes + 1, likes = MAX(0, likes - 1) WHERE name = ?",
                    (pack_name,),
                )
            current = reaction
    else:
        c.execute(
            "INSERT INTO catalog_reactions (user_id, pack_name, reaction) VALUES (?, ?, ?)",
            (user_id, pack_name, reaction),
        )
        if reaction == "like":
            c.execute(
                "UPDATE catalog_packs SET likes = likes + 1 WHERE name = ?",
                (pack_name,),
            )
        else:
            c.execute(
                "UPDATE catalog_packs SET dislikes = dislikes + 1 WHERE name = ?",
                (pack_name,),
            )
        current = reaction

    conn.commit()
    c.execute(
        "SELECT likes, dislikes FROM catalog_packs WHERE name = ?", (pack_name,)
    )
    row = c.fetchone()
    conn.close()
    return {
        "likes": row["likes"] if row else 0,
        "dislikes": row["dislikes"] if row else 0,
        "current": current,
    }


def catalog_get_user_reaction(user_id: int, pack_name: str) -> str | None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT reaction FROM catalog_reactions WHERE user_id = ? AND pack_name = ?",
        (user_id, pack_name),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None
