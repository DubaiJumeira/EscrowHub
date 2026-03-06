from __future__ import annotations

import logging
import time

from infra.chain_adapters.eth_rpc import EthRpcAdapter
from infra.db.database import get_connection, init_db
from wallet_service import WalletService

LOGGER = logging.getLogger(__name__)


def _get_cursor(conn, chain: str, default: int) -> int:
    row = conn.execute("SELECT cursor FROM chain_scan_state WHERE chain_family=?", (chain,)).fetchone()
    return int(row["cursor"]) if row else default


def _set_cursor(conn, chain: str, cursor: int) -> None:
    conn.execute(
        "INSERT INTO chain_scan_state(chain_family,cursor) VALUES(?,?) ON CONFLICT(chain_family) DO UPDATE SET cursor=excluded.cursor, updated_at=CURRENT_TIMESTAMP",
        (chain, str(cursor)),
    )


def run_once(address_user_map: dict[str, int], batch_size: int = 100) -> int:
    conn = get_connection()
    init_db(conn)
    credited = 0
    try:
        wallet = WalletService(conn)
        adapter = EthRpcAdapter(address_user_map)
        latest = adapter.get_latest_block() if adapter.rpc_url else 0
        if latest == 0:
            return 0
        start = _get_cursor(conn, "ETHEREUM", max(0, latest - batch_size)) + 1
        end = min(start + batch_size - 1, latest)

        delay = 0.5
        for attempt in range(3):
            try:
                deposits = adapter.fetch_deposits_between(start, end)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(delay)
                delay *= 2
        else:
            deposits = []

        for dep in deposits:
            if wallet.credit_deposit_if_confirmed(dep.user_id, dep.asset, dep.amount, dep.txid, dep.unique_key, "ETHEREUM", dep.confirmations, dep.finalized):
                credited += 1
        _set_cursor(conn, "ETHEREUM", end)
        conn.commit()
        LOGGER.info("ETH watcher scanned %s-%s credited=%s", start, end, credited)
        return credited
    except Exception:
        conn.rollback()
        LOGGER.exception("eth watcher failed")
        raise
    finally:
        conn.close()
