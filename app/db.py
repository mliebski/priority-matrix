"""SQLite storage. One file on the container's /data volume; no database server needed."""
import json
import os
import sqlite3
from collections.abc import Iterator

SCHEMA = """
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
"""


def path() -> str:
    return os.environ.get("DB_PATH", "/data/matrix.db")


def connect() -> sqlite3.Connection:
    # FastAPI may run a request's dependency and route in different threads, never at once.
    conn = sqlite3.connect(path(), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


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


def to_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["chat"] = json.loads(item["chat"])
    item["suggestion"] = json.loads(item["suggestion"]) if item["suggestion"] else None
    return item


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return to_item(row) if row else None
