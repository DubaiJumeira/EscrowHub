from __future__ import annotations

import logging
import time

from infra.chain_adapters.btc_blockstream import BlockstreamUtxoAdapter
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


def run_once(asset: str, address_user_map: dict[str, int]) -> int:
    conn = get_connection()
    init_db(conn)
    credited = 0
    try:
        wallet = WalletService(conn)
        adapter = BlockstreamUtxoAdapter(asset=asset, address_user_map=address_user_map)
        delay = 0.5
        for attempt in range(3):
            try:
                deposits = adapter.fetch_deposits()
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(delay)
                delay *= 2
        else:
            deposits = []

        for dep in deposits:
            if wallet.credit_deposit_if_confirmed(dep.user_id, dep.asset, dep.amount, dep.txid, dep.unique_key, asset.upper(), dep.confirmations, dep.finalized):
                credited += 1

        # Blockstream API tx endpoint doesn't expose a monotonic cursor here; use current timestamp surrogate.
        _set_cursor(conn, asset.upper(), int(time.time()))
        conn.commit()
        LOGGER.info("%s watcher credited=%s", asset, credited)
        return credited
    except Exception:
        conn.rollback()
        LOGGER.exception("%s watcher failed", asset)
        raise
    finally:
        conn.close()
