from __future__ import annotations

import logging
from dataclasses import dataclass

from infra.db.database import get_connection, init_db
from error_sanitizer import sanitize_runtime_error
from signer.signer_service import SignerService
from wallet_service import WalletService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightStatus:
    service_name: str
    ok: bool = True
    deposit_issuance_ready: bool = False
    withdrawal_provider_ready: bool = False
    signer_ready: bool = False
    reasons: tuple[str, ...] = ()
    deposit_issuance_error: str | None = None
    route_integrity_ready: bool = False
    signer_loop_degraded: bool = False


class PreflightIntegrityError(RuntimeError):
    """Raised when startup integrity checks fail closed."""

    def __init__(self, status: PreflightStatus) -> None:
        self.status = status
        super().__init__("; ".join(status.reasons) or "startup integrity check failed")


def run_startup_preflight(service_name: str) -> PreflightStatus:
    """Service-scoped startup checks required before serving traffic."""
    conn = get_connection()
    reasons: list[str] = []
    try:
        route_integrity_ready = True
        fatal_integrity = False
        try:
            init_db(conn)
        except Exception as exc:
            route_integrity_ready = False
            fatal_integrity = True
            reasons.append(f"route integrity failed: {sanitize_runtime_error(exc)}")
        wallet = WalletService(conn)
        try:
            wallet.ensure_wallet_route_integrity()
            wallet.verify_address_derivation_consistency(sample_size=None)
        except Exception as exc:
            route_integrity_ready = False
            reasons.append(f"route integrity failed: {sanitize_runtime_error(exc)}")
            fatal_integrity = True

        deposit_ready = False
        deposit_error = None
        withdrawal_ready = False
        signer_ready = False

        if service_name == "bot":
            try:
                wallet.assert_startup_deposit_issuance_ready()
                deposit_ready = True
            except Exception as exc:
                deposit_error = sanitize_runtime_error(exc)
                reasons.append(f"deposit provider unavailable: {deposit_error}")
                LOGGER.warning("startup preflight (%s): bot degraded mode: %s", service_name, deposit_error)
            status = PreflightStatus(
                service_name=service_name,
                ok=True,
                deposit_issuance_ready=deposit_ready,
                withdrawal_provider_ready=False,
                signer_ready=False,
                reasons=tuple(reasons),
                deposit_issuance_error=deposit_error,
                route_integrity_ready=route_integrity_ready,
                signer_loop_degraded=bool(reasons),
            )
            if fatal_integrity:
                # WARNING: Route-integrity failures are fatal to prevent startup with tampered/colliding deposit routes.
                raise PreflightIntegrityError(status)
            return status

        if service_name == "signer":
            signer = SignerService()
            signer_ready, signer_reason = signer.readiness()
            withdrawal_ready = signer_ready
            if not signer_ready:
                reasons.append(signer_reason or "withdrawal provider/signer not ready")
            ok = True
            status = PreflightStatus(
                service_name=service_name,
                ok=ok,
                deposit_issuance_ready=False,
                withdrawal_provider_ready=withdrawal_ready,
                signer_ready=signer_ready,
                reasons=tuple(reasons),
                route_integrity_ready=route_integrity_ready,
                signer_loop_degraded=bool(reasons),
            )
            if fatal_integrity:
                # WARNING: Route-integrity failures are fatal to prevent signer startup against unsafe routing state.
                raise PreflightIntegrityError(status)
            return status

        status = PreflightStatus(
            service_name=service_name,
            ok=True,
            deposit_issuance_ready=False,
            withdrawal_provider_ready=False,
            signer_ready=False,
            reasons=tuple(reasons),
            route_integrity_ready=route_integrity_ready,
            signer_loop_degraded=bool(reasons),
        )
        if fatal_integrity:
            # WARNING: Route-integrity failures are fatal for watchers and other services to fail closed on tampering risk.
            raise PreflightIntegrityError(status)
        return status
    finally:
        conn.close()
