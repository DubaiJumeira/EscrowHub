from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("SQLITE_DB_PATH", str(Path("escrowhub.db").resolve()))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    schema = Path("infra/db/schema.sql").read_text()
    conn.executescript(schema)
    watcher_cols = {row["name"] for row in conn.execute("PRAGMA table_info(watcher_status)").fetchall()}
    if "cursor" not in watcher_cols:
        conn.execute("ALTER TABLE watcher_status ADD COLUMN cursor INTEGER")
    bot_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bots)").fetchall()}
    if "telegram_username" not in bot_cols:
        conn.execute("ALTER TABLE bots ADD COLUMN telegram_username TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bots_telegram_username ON bots(telegram_username)")
    conn.commit()
