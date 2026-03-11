from __future__ import annotations

import logging
import os
import time

from infra.db.database import get_connection, init_db
from error_sanitizer import sanitize_runtime_error
from runtime_preflight import PreflightIntegrityError, run_startup_preflight
from wallet_service import WalletService
from watcher_status_service import upsert_watcher_status
from watchers.btc_watcher import run_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_ltc_watcher")


def _address_map(conn) -> dict[str, int]:
    wallet = WalletService(conn)
    return wallet.monitored_deposit_address_map(["LTC"])


def main() -> None:
    enabled = os.getenv("LTC_WATCHER_ENABLED", "false").lower() == "true"
    if not enabled:
        LOGGER.info("LTC watcher disabled by config")
        conn = get_connection()
        init_db(conn)
        try:
            upsert_watcher_status(conn, "ltc_watcher", success=False, error="disabled by config", health="disabled")
            conn.commit()
        finally:
            conn.close()
        return
    interval = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))
    LOGGER.info("starting LTC watcher loop with interval=%ss", interval)
    try:
        run_startup_preflight("ltc_watcher")
    except PreflightIntegrityError as exc:
        conn = get_connection(); init_db(conn)
        try:
            upsert_watcher_status(conn, "ltc_watcher", success=False, error="; ".join(exc.status.reasons) or str(exc), health="fatal_startup_blocked")
            conn.commit()
        finally:
            conn.close()
        LOGGER.error("ltc watcher startup aborted by fatal integrity preflight: %s", "; ".join(exc.status.reasons) or str(exc))
        raise

    while True:
        conn = get_connection()
        init_db(conn)
        try:
            start = time.time()
            credited = run_once("LTC", _address_map(conn))
            upsert_watcher_status(conn, "ltc_watcher", success=True, health="ok")
            conn.commit()
            LOGGER.info("ltc watcher cycle success credited=%s duration=%.2fs", credited, time.time() - start)
        except Exception as exc:
            conn.rollback()
            upsert_watcher_status(conn, "ltc_watcher", success=False, error=sanitize_runtime_error(exc), health="transient_failure")
            conn.commit()
            LOGGER.exception("ltc watcher cycle failed")
        finally:
            conn.close()
        time.sleep(interval)


if __name__ == "__main__":
    main()
