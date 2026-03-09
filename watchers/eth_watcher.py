from __future__ import annotations

import logging

from infra.chain_adapters.eth_rpc import EthRpcAdapter
from infra.db.database import get_connection, init_db
from wallet_service import WalletService
from watcher_status_service import write_watcher_cursor
from watchers.notify import notify_deposit_credited

LOGGER = logging.getLogger(__name__)


def run_once(address_user_map: dict[str, int]) -> int:
    conn = get_connection()
    init_db(conn)
    try:
        wallet = WalletService(conn)
        adapter = EthRpcAdapter(address_user_map, conn=conn)
        credited = 0
        deposits, finalized = adapter.fetch_deposits()
        for dep in deposits:
            try:
                did_credit = wallet.credit_deposit_if_confirmed(
                    dep.user_id,
                    dep.asset,
                    dep.amount,
                    dep.txid,
                    dep.unique_key,
                    "ETHEREUM",
                    dep.confirmations,
                    dep.finalized,
                )
                if did_credit:
                    credited += 1
                    notify_deposit_credited(conn, dep.user_id, dep.asset, dep.amount, wallet.available_balance(dep.user_id, dep.asset))
            except Exception:
                LOGGER.exception("failed to ingest ETH deposit event unique_key=%s", dep.unique_key)
        if finalized is not None:
            write_watcher_cursor(conn, "eth_watcher", finalized)
        conn.commit()
        return credited
    except Exception:
        conn.rollback()
        LOGGER.exception("eth watcher failed")
        raise
    finally:
        conn.close()
