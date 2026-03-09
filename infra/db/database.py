from __future__ import annotations

import sqlite3
from pathlib import Path

from config.settings import Settings


def _db_path() -> str:
    configured = (Settings.sqlite_db_path or "").strip()
    if configured:
        return configured
    return str(Path("escrowhub.db").resolve())


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
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


    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_wallet_addresses_chain_fp_user_guard_ins BEFORE INSERT ON wallet_addresses "
        "WHEN COALESCE(NEW.address_fingerprint,'') != '' AND EXISTS (SELECT 1 FROM wallet_addresses w WHERE w.chain_family=NEW.chain_family AND w.address_fingerprint=NEW.address_fingerprint AND w.user_id != NEW.user_id) "
        "BEGIN SELECT RAISE(ABORT, 'wallet chain/address fingerprint already assigned to a different user'); END;"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_wallet_addresses_chain_fp_user_guard_upd BEFORE UPDATE OF chain_family, address_fingerprint, user_id ON wallet_addresses "
        "WHEN COALESCE(NEW.address_fingerprint,'') != '' AND EXISTS (SELECT 1 FROM wallet_addresses w WHERE w.id != NEW.id AND w.chain_family=NEW.chain_family AND w.address_fingerprint=NEW.address_fingerprint AND w.user_id != NEW.user_id) "
        "BEGIN SELECT RAISE(ABORT, 'wallet chain/address fingerprint already assigned to a different user'); END;"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_wallet_addresses_provider_ref_guard_ins BEFORE INSERT ON wallet_addresses "
        "WHEN COALESCE(NEW.provider_origin, '') != '' AND COALESCE(NEW.provider_ref, '') != '' AND "
        "EXISTS (SELECT 1 FROM wallet_addresses w WHERE w.provider_origin=NEW.provider_origin AND w.provider_ref=NEW.provider_ref AND (w.user_id != NEW.user_id OR COALESCE(w.address_fingerprint,'') != COALESCE(NEW.address_fingerprint,''))) "
        "BEGIN SELECT RAISE(ABORT, 'provider_ref cannot be rebound to another route'); END;"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_wallet_addresses_provider_ref_guard_upd BEFORE UPDATE OF provider_origin, provider_ref, user_id, address_fingerprint ON wallet_addresses "
        "WHEN COALESCE(NEW.provider_origin, '') != '' AND COALESCE(NEW.provider_ref, '') != '' AND "
        "EXISTS (SELECT 1 FROM wallet_addresses w WHERE w.id != NEW.id AND w.provider_origin=NEW.provider_origin AND w.provider_ref=NEW.provider_ref AND (w.user_id != NEW.user_id OR COALESCE(w.address_fingerprint,'') != COALESCE(NEW.address_fingerprint,''))) "
        "BEGIN SELECT RAISE(ABORT, 'provider_ref cannot be rebound to another route'); END;"
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




def _wallet_route_collisions(conn: sqlite3.Connection) -> list[str]:
    collisions: list[str] = []
    addr_rows = conn.execute(
        """
        SELECT chain_family, address_fingerprint, GROUP_CONCAT(DISTINCT user_id) AS users, COUNT(DISTINCT user_id) AS user_count
        FROM wallet_addresses
        WHERE COALESCE(address_fingerprint,'') != ''
        GROUP BY chain_family, address_fingerprint
        HAVING COUNT(DISTINCT user_id) > 1
        ORDER BY chain_family, address
        """
    ).fetchall()
    for row in addr_rows:
        collisions.append(f"chain/fingerprint collision family={row['chain_family']} users={row['users']}")

    ref_rows = conn.execute(
        """
        SELECT provider_origin, provider_ref, GROUP_CONCAT(DISTINCT user_id) AS users, COUNT(DISTINCT user_id) AS user_count, COUNT(DISTINCT COALESCE(address_fingerprint,'')) AS address_count
        FROM wallet_addresses
        WHERE COALESCE(provider_origin, '') != '' AND COALESCE(provider_ref, '') != ''
        GROUP BY provider_origin, provider_ref
        HAVING COUNT(DISTINCT user_id) > 1 OR COUNT(DISTINCT COALESCE(address_fingerprint,'')) > 1
        ORDER BY provider_origin, provider_ref
        """
    ).fetchall()
    for row in ref_rows:
        collisions.append(
            f"provider_ref collision origin={row['provider_origin']} ref={row['provider_ref']} users={row['users']} address_count={row['address_count']}"
        )
    return collisions

def init_db(conn: sqlite3.Connection) -> None:
    schema = Path("infra/db/schema.sql").read_text()
    conn.executescript(schema)
    watcher_cols = {row["name"] for row in conn.execute("PRAGMA table_info(watcher_status)").fetchall()}
    if "cursor" not in watcher_cols:
        conn.execute("ALTER TABLE watcher_status ADD COLUMN cursor INTEGER")

    withdrawal_cols = {row["name"] for row in conn.execute("PRAGMA table_info(withdrawals)").fetchall()}
    wallet_cols = {row["name"] for row in conn.execute("PRAGMA table_info(wallet_addresses)").fetchall()}
    if "provider_origin" not in wallet_cols:
        conn.execute("ALTER TABLE wallet_addresses ADD COLUMN provider_origin TEXT")
    if "provider_ref" not in wallet_cols:
        conn.execute("ALTER TABLE wallet_addresses ADD COLUMN provider_ref TEXT")
    if "address_fingerprint" not in wallet_cols:
        conn.execute("ALTER TABLE wallet_addresses ADD COLUMN address_fingerprint TEXT")

    if "failure_reason" not in withdrawal_cols:
        conn.execute("ALTER TABLE withdrawals ADD COLUMN failure_reason TEXT")

    table_sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='withdrawals'").fetchone()
    table_sql = str(table_sql_row["sql"] or "") if table_sql_row else ""
    if "signer_retry" not in table_sql:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER,
              asset TEXT NOT NULL CHECK(asset IN ('BTC','LTC','ETH','USDT')),
              amount TEXT NOT NULL,
              destination_address TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('pending','broadcasted','failed','signer_retry')),
              txid TEXT,
              failure_reason TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO withdrawals_new(id,user_id,asset,amount,destination_address,status,txid,failure_reason,created_at)
            SELECT id,user_id,asset,amount,destination_address,status,txid,failure_reason,created_at FROM withdrawals
            """
        )
        conn.execute("DROP TABLE withdrawals")
        conn.execute("ALTER TABLE withdrawals_new RENAME TO withdrawals")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_user_status_created ON withdrawals(user_id, status, created_at)")

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_addresses_chain_fingerprint ON wallet_addresses(chain_family, address_fingerprint)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_addresses_provider_ref ON wallet_addresses(provider_origin, provider_ref) WHERE COALESCE(provider_origin, '') != '' AND COALESCE(provider_ref, '') != ''")

    from wallet_service import WalletService

    WalletService(conn).ensure_wallet_route_integrity()

    wallet_collisions = _wallet_route_collisions(conn)
    if wallet_collisions:
        details = "; ".join(wallet_collisions[:5])
        raise RuntimeError(
            "wallet address routing collisions detected during migration. "
            f"Resolve collisions before startup: {details}"
        )

    _apply_security_constraints(conn)
    conn.commit()
