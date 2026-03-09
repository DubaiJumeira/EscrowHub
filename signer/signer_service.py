from __future__ import annotations

import hashlib
import logging
import os
from decimal import Decimal
from urllib.request import Request, urlopen

from config.settings import Settings
from signer.errors import AmbiguousBroadcastError, DeterministicSigningError, RetryableSignerError
from signer.withdrawal_provider import (
    DisabledWithdrawalProvider,
    HttpWithdrawalProvider,
    WithdrawalExecutionRequest,
    WithdrawalExecutionResult,
    WithdrawalProvider,
    WithdrawalReconciliationRequest,
)

LOGGER = logging.getLogger(__name__)


class DisabledSignerProvider:
    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        raise RuntimeError("signing provider disabled")


class VaultSignerProvider(DisabledWithdrawalProvider):
    """Legacy placeholder kept for compatibility; transit-only mode is non-production."""

    provider_origin = "vault_legacy"

    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        req = Request("http://vault.invalid/v1/transit/sign/escrowhub", method="POST")
        try:
            with urlopen(req, timeout=5):
                pass
        except Exception:
            pass
        # WARNING: Vault transit-only flow is intentionally fail-closed to prevent fabricated txids.
        raise DeterministicSigningError("incomplete signer path: no real transaction builder+broadcaster integration")


class SignerService:
    INFLIGHT_STATUSES = ("pending", "submitted", "broadcasted", "signer_retry")

    def __init__(self) -> None:
        provider = os.getenv("WITHDRAWAL_PROVIDER", os.getenv("SIGNER_PROVIDER", "http")).lower().strip()
        if provider == "http":
            self.provider: WithdrawalProvider | object = HttpWithdrawalProvider()
        elif provider == "vault":
            self.provider = VaultSignerProvider()
        else:
            if Settings.is_production:
                raise RuntimeError("Only real external withdrawal providers are allowed in production")
            self.provider = DisabledWithdrawalProvider()

    def readiness(self) -> tuple[bool, str | None]:
        if not Settings.withdrawals_enabled:
            return False, "WITHDRAWALS_ENABLED=false"
        if hasattr(self.provider, "is_ready"):
            return self.provider.is_ready()  # type: ignore[no-any-return]
        return True, None

    def _idempotency_key(self, withdrawal_id: int, user_id: int, asset: str, amount: str, destination: str) -> str:
        payload = f"{withdrawal_id}|{user_id}|{asset}|{amount}|{destination}".encode()
        digest = hashlib.sha256(payload).hexdigest()
        return f"wd:{withdrawal_id}:{digest[:24]}"

    def _map_result(self, result: WithdrawalExecutionResult) -> tuple[str, str | None]:
        status = (result.status or "").strip().lower()
        if status == "submitted":
            return "submitted", None
        if status in {"broadcasted", "confirmed"}:
            if not (result.txid or "").strip():
                raise RetryableSignerError("provider returned onchain status without txid")
            return status, str(result.txid).strip()
        if status in {"rejected", "permanent_failure"}:
            raise DeterministicSigningError((result.message or "withdrawal rejected by provider")[:500])
        if status in {"retryable", "ambiguous", "unknown"}:
            raise RetryableSignerError((result.message or "withdrawal outcome ambiguous")[:500])
        raise RetryableSignerError(f"unknown provider status: {status or 'empty'}")

    def process_withdrawals(self, wallet_service) -> int:
        ready, reason = self.readiness()
        if not ready:
            LOGGER.info("withdrawal processing skipped: %s", reason or "not ready")
            return 0
        processed = 0
        for w in wallet_service.pending_withdrawals():
            processed += self._execute_single(wallet_service, w)
        for w in wallet_service.unresolved_withdrawals_for_reconcile(limit=100):
            processed += self._reconcile_single(wallet_service, w)
        return processed

    def process_pending_withdrawals(self, wallet_service) -> int:
        return self.process_withdrawals(wallet_service)

    def _execute_single(self, wallet_service, w: dict) -> int:
        try:
            idem = str(w.get("idempotency_key") or "") or self._idempotency_key(int(w["id"]), int(w["user_id"]), str(w["asset"]), str(w["amount"]), str(w["destination_address"]))
            wallet_service.persist_withdrawal_idempotency(int(w["id"]), idem)
            request = WithdrawalExecutionRequest(
                withdrawal_id=int(w["id"]), user_id=int(w["user_id"]), asset=str(w["asset"]).upper().strip(),
                amount=str(Decimal(str(w["amount"]))), destination_address=str(w["destination_address"]), idempotency_key=idem,
            )
            if hasattr(self.provider, "execute_withdrawal"):
                result = self.provider.execute_withdrawal(request)
                status, txid = self._map_result(result)
                wallet_service.record_withdrawal_provider_result(int(w["id"]), getattr(self.provider, "provider_origin", "external_http"), idem, result)
                if status == "submitted":
                    wallet_service.mark_withdrawal_submitted(int(w["id"]), result.provider_ref, result.external_status, result.submitted_at)
                elif status == "broadcasted":
                    wallet_service.mark_withdrawal_broadcasted(int(w["id"]), txid or "", result.provider_ref, result.external_status, result.broadcasted_at)
                elif status == "confirmed":
                    wallet_service.mark_withdrawal_confirmed(int(w["id"]), txid or "", result.provider_ref, result.external_status)
            else:
                txid = self.provider.sign_and_broadcast(request.asset, request.destination_address, request.amount, user_id=request.user_id)
                wallet_service.mark_withdrawal_broadcasted(int(w["id"]), str(txid))
            return 1
        except Exception as exc:
            LOGGER.exception("withdrawal processing failed id=%s", w["id"])
            if isinstance(exc, (AmbiguousBroadcastError, RetryableSignerError)):
                wallet_service.mark_withdrawal_signer_retry(int(w["id"]), str(exc))
            elif isinstance(exc, DeterministicSigningError):
                wallet_service.mark_withdrawal_failed(int(w["id"]), str(exc))
            else:
                wallet_service.mark_withdrawal_signer_retry(int(w["id"]), str(exc))
            return 0

    def _reconcile_single(self, wallet_service, w: dict) -> int:
        if not hasattr(self.provider, "reconcile_withdrawal"):
            return 0
        try:
            req = WithdrawalReconciliationRequest(withdrawal_id=int(w["id"]), idempotency_key=str(w.get("idempotency_key") or ""), provider_ref=str(w.get("provider_ref") or "") or None)
            result = self.provider.reconcile_withdrawal(req)
            status, txid = self._map_result(result)
            wallet_service.record_withdrawal_provider_result(int(w["id"]), getattr(self.provider, "provider_origin", "external_http"), req.idempotency_key, result)
            wallet_service.mark_withdrawal_reconciled(int(w["id"]))
            if status == "submitted":
                wallet_service.mark_withdrawal_submitted(int(w["id"]), result.provider_ref, result.external_status, result.submitted_at)
            elif status == "broadcasted":
                wallet_service.mark_withdrawal_broadcasted(int(w["id"]), txid or "", result.provider_ref, result.external_status, result.broadcasted_at)
            elif status == "confirmed":
                wallet_service.mark_withdrawal_confirmed(int(w["id"]), txid or "", result.provider_ref, result.external_status)
            return 1
        except Exception as exc:
            wallet_service.mark_withdrawal_signer_retry(int(w["id"]), str(exc))
            wallet_service.mark_withdrawal_reconciled(int(w["id"]))
            return 0
