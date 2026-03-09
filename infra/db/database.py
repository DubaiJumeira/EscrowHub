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


def _apply_security_constraints(conn: sqlite3.Connection) -> None:
    asset_check = "NEW.asset NOT IN ('BTC','LTC','ETH','USDT')"
    for table in ("wallet_addresses", "deposits", "withdrawals", "escrows", "escrow_locks", "ledger_entries"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_asset_check_ins BEFORE INSERT ON {table} "
            f"WHEN {asset_check} BEGIN SELECT RAISE(ABORT, 'invalid asset'); END;"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_asset_check_upd BEFORE UPDATE OF asset ON {table} "
            f"WHEN {asset_check} BEGIN SELECT RAISE(ABORT, 'invalid asset'); END;"
        )

    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_bots_username_check_ins BEFORE INSERT ON bots "
        "WHEN NEW.telegram_username IS NOT NULL AND (NEW.telegram_username != lower(NEW.telegram_username) OR instr(NEW.telegram_username, '@') > 0) "
        "BEGIN SELECT RAISE(ABORT, 'telegram_username must be lowercase and must not include @'); END;"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_bots_username_check_upd BEFORE UPDATE OF telegram_username ON bots "
        "WHEN NEW.telegram_username IS NOT NULL AND (NEW.telegram_username != lower(NEW.telegram_username) OR instr(NEW.telegram_username, '@') > 0) "
        "BEGIN SELECT RAISE(ABORT, 'telegram_username must be lowercase and must not include @'); END;"
    )

    for table, family_col in (("wallet_addresses", "chain_family"), ("deposits", "chain_family")):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_asset_chain_check_ins BEFORE INSERT ON {table} "
            f"WHEN NOT ((NEW.asset IN ('BTC','LTC') AND NEW.{family_col}=NEW.asset) OR (NEW.asset IN ('ETH','USDT') AND NEW.{family_col}='ETHEREUM')) "
            "BEGIN SELECT RAISE(ABORT, 'invalid asset/chain_family combination'); END;"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_asset_chain_check_upd BEFORE UPDATE OF asset, {family_col} ON {table} "
            f"WHEN NOT ((NEW.asset IN ('BTC','LTC') AND NEW.{family_col}=NEW.asset) OR (NEW.asset IN ('ETH','USDT') AND NEW.{family_col}='ETHEREUM')) "
            "BEGIN SELECT RAISE(ABORT, 'invalid asset/chain_family combination'); END;"
        )


def _normalized_username_collisions(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT norm, GROUP_CONCAT(id) ids, COUNT(*) c
        FROM (
          SELECT id, lower(ltrim(trim(telegram_username),'@')) AS norm
          FROM bots
          WHERE telegram_username IS NOT NULL AND trim(telegram_username) != ''
        )
        GROUP BY norm
        HAVING c > 1
        ORDER BY norm ASC
        """
    ).fetchall()
    return [f"{row['norm']} (bot_ids: {row['ids']})" for row in rows]


def init_db(conn: sqlite3.Connection) -> None:
    schema = Path("infra/db/schema.sql").read_text()
    conn.executescript(schema)
    watcher_cols = {row["name"] for row in conn.execute("PRAGMA table_info(watcher_status)").fetchall()}
    if "cursor" not in watcher_cols:
        conn.execute("ALTER TABLE watcher_status ADD COLUMN cursor INTEGER")

    withdrawal_cols = {row["name"] for row in conn.execute("PRAGMA table_info(withdrawals)").fetchall()}
    if "failure_reason" not in withdrawal_cols:
        conn.execute("ALTER TABLE withdrawals ADD COLUMN failure_reason TEXT")

    bot_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bots)").fetchall()}
    if "telegram_username" not in bot_cols:
        conn.execute("ALTER TABLE bots ADD COLUMN telegram_username TEXT")

    collisions = _normalized_username_collisions(conn)
    if collisions:
        details = "; ".join(collisions[:5])
        raise RuntimeError(
            "telegram_username normalization collision detected during migration. "
            f"Resolve duplicates before startup (normalized -> bot ids): {details}"
        )

    conn.execute("UPDATE bots SET telegram_username=lower(ltrim(trim(telegram_username),'@')) WHERE telegram_username IS NOT NULL")
    conn.execute("UPDATE bots SET telegram_username=NULL WHERE telegram_username=''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bots_telegram_username ON bots(telegram_username)")
    _apply_security_constraints(conn)
    conn.commit()
