from __future__ import annotations

import logging
from dataclasses import dataclass

from infra.db.database import get_connection, init_db
from wallet_service import WalletService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightStatus:
    service_name: str
    deposit_issuance_ready: bool
    deposit_issuance_error: str | None = None


def run_startup_preflight(service_name: str) -> PreflightStatus:
    """Service-scoped startup checks required before serving traffic."""
    conn = get_connection()
    try:
        init_db(conn)
        wallet = WalletService(conn)
        wallet.verify_address_derivation_consistency(sample_size=None)

        if service_name == "bot":
            try:
                wallet.assert_startup_deposit_issuance_ready()
                LOGGER.info("startup preflight (%s): deposit issuance readiness check passed", service_name)
                return PreflightStatus(service_name=service_name, deposit_issuance_ready=True)
            except Exception as exc:
                LOGGER.warning(
                    "startup preflight (%s): deposit issuance unavailable; bot will run in degraded mode", service_name
                )
                return PreflightStatus(
                    service_name=service_name,
                    deposit_issuance_ready=False,
                    deposit_issuance_error=str(exc),
                )

        LOGGER.info("startup preflight (%s): derivation consistency check passed", service_name)
        return PreflightStatus(service_name=service_name, deposit_issuance_ready=False)
    finally:
        conn.close()
