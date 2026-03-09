from __future__ import annotations

import logging
import os
import time

from infra.db.database import get_connection, init_db
from signer.signer_service import SignerService
from runtime_preflight import run_startup_preflight
from wallet_service import WalletService
from watcher_status_service import upsert_watcher_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_signer")


def main() -> None:
    interval = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))
    preflight = run_startup_preflight("signer")
    if preflight is not None and not preflight.signer_ready:
        LOGGER.warning("signer preflight degraded: %s", "; ".join(preflight.reasons) or "not ready")
    LOGGER.info("starting signer loop interval=%ss", interval)
    while True:
        conn = get_connection()
        init_db(conn)
        try:
            start = time.time()
            wallet = WalletService(conn)
            count = SignerService().process_pending_withdrawals(wallet)
            upsert_watcher_status(conn, "signer_loop", success=True)
            conn.commit()
            LOGGER.info("signer cycle success processed=%s duration=%.2fs", count, time.time() - start)
        except Exception as exc:
            conn.rollback()
            upsert_watcher_status(conn, "signer_loop", success=False, error=str(exc))
            conn.commit()
            LOGGER.exception("signer cycle failed")
        finally:
            conn.close()
        time.sleep(interval)


if __name__ == "__main__":
    main()
