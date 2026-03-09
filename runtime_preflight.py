from __future__ import annotations

from infra.db.database import get_connection, init_db
from wallet_service import WalletService


def run_startup_preflight(service_name: str) -> None:
    """Fail-closed startup checks required before serving traffic."""
    conn = get_connection()
    try:
        init_db(conn)
        wallet = WalletService(conn)
        wallet.verify_address_derivation_consistency(sample_size=None)
        wallet.assert_startup_deposit_issuance_ready()
    finally:
        conn.close()
