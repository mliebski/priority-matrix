"""SQLite storage. One file on the container's /data volume; no database server needed."""
import json
import os
import sqlite3
from collections.abc import Iterator

# Schema changes are appended here, never edited. PRAGMA user_version records how many
# have run, so an existing database is upgraded the next time the app starts.
MIGRATIONS = ["""
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    due TEXT,                        -- YYYY-MM-DD
    quadrant TEXT,                   -- do, schedule, delegate, drop; NULL while in the inbox
    reason TEXT NOT NULL DEFAULT '', -- why it was placed there, when the coach's suggestion was taken
    chat TEXT NOT NULL DEFAULT '[]',
    suggestion TEXT,
    done_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
""", """
-- Bumped whenever the coach conversation changes, so a stale turn can be refused.
ALTER TABLE items ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
"""]


def path() -> str:
    return os.environ.get("DB_PATH", "/data/matrix.db")


def connect() -> sqlite3.Connection:
    # FastAPI may run a request's dependency and route in different threads, never at once.
    conn = sqlite3.connect(path(), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't wait for a write; persists in the file
        done = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[done:], start=done + 1):
            # executescript commits first, so the version bump rides in the same script.
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    finally:
        conn.close()


def session() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, committed if the request succeeds."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


def to_item(row: sqlite3.Row, with_chat: bool = True) -> dict:
    """with_chat=False is for lists: the conversation is fetched when an item is opened."""
    item = dict(row)
    item["chat"] = json.loads(item["chat"]) if with_chat else None
    item["suggestion"] = json.loads(item["suggestion"]) if item["suggestion"] else None
    return item


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return to_item(row) if row else None
