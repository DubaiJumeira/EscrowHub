from __future__ import annotations

import logging
import os
import time

from infra.db.database import get_connection, init_db
from runtime_preflight import run_startup_preflight
from wallet_service import WalletService
from watcher_status_service import upsert_watcher_status
from watchers.btc_watcher import run_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_btc_watcher")


def _address_map(conn) -> dict[str, int]:
    wallet = WalletService(conn)
    return wallet.monitored_deposit_address_map(["BTC"])


def main() -> None:
    enabled = os.getenv("BTC_WATCHER_ENABLED", "true").lower() == "true"
    if not enabled:
        LOGGER.info("BTC watcher disabled by config")
        return
    interval = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))
    LOGGER.info("starting BTC watcher loop with interval=%ss", interval)
    run_startup_preflight("btc_watcher")

    while True:
        conn = get_connection()
        init_db(conn)
        try:
            start = time.time()
            credited = run_once("BTC", _address_map(conn))
            upsert_watcher_status(conn, "btc_watcher", success=True)
            conn.commit()
            LOGGER.info("btc watcher cycle success credited=%s duration=%.2fs", credited, time.time() - start)
        except Exception as exc:
            conn.rollback()
            upsert_watcher_status(conn, "btc_watcher", success=False, error=str(exc))
            conn.commit()
            LOGGER.exception("btc watcher cycle failed")
        finally:
            conn.close()
        time.sleep(interval)


if __name__ == "__main__":
    main()
