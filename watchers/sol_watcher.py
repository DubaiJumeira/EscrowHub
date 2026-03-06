from __future__ import annotations

import logging
import time

from infra.chain_adapters.sol_rpc import SolRpcAdapter
from infra.db.database import get_connection, init_db
from wallet_service import WalletService

LOGGER = logging.getLogger(__name__)


def _get_cursor(conn, chain: str) -> str | None:
    row = conn.execute("SELECT cursor FROM chain_scan_state WHERE chain_family=?", (chain,)).fetchone()
    return row["cursor"] if row else None


def _set_cursor(conn, chain: str, cursor: str) -> None:
    conn.execute(
        "INSERT INTO chain_scan_state(chain_family,cursor) VALUES(?,?) ON CONFLICT(chain_family) DO UPDATE SET cursor=excluded.cursor, updated_at=CURRENT_TIMESTAMP",
        (chain, cursor),
    )


def run_once(address_user_map: dict[str, int]) -> int:
    conn = get_connection()
    init_db(conn)
    try:
        wallet = WalletService(conn)
        adapter = SolRpcAdapter(address_user_map)
        cursor = _get_cursor(conn, "SOL")
        delay = 0.5
        for attempt in range(3):
            try:
                deposits, newest = adapter.fetch_deposits_since_signature(cursor)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(delay)
                delay *= 2
        else:
            deposits, newest = [], cursor

        credited = 0
        for dep in deposits:
            if wallet.credit_deposit_if_confirmed(dep.user_id, dep.asset, dep.amount, dep.txid, dep.unique_key, "SOL", dep.confirmations, dep.finalized):
                credited += 1
        if newest:
            _set_cursor(conn, "SOL", newest)
        conn.commit()
        LOGGER.info("SOL watcher credited=%s", credited)
        return credited
    except Exception:
        conn.rollback()
        LOGGER.exception("sol watcher failed")
        raise
    finally:
        conn.close()
