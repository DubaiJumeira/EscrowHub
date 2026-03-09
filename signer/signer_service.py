from __future__ import annotations

import logging
import os
from decimal import Decimal
from urllib.request import Request, urlopen

from config.settings import Settings
from error_sanitizer import sanitize_runtime_error
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

    def _reconcile_intervals(self) -> tuple[int, int, int]:
        submitted = max(15, int(os.getenv("WITHDRAWAL_RECONCILE_SUBMITTED_INTERVAL_SECONDS", "45")))
        broadcasted = max(30, int(os.getenv("WITHDRAWAL_RECONCILE_BROADCASTED_INTERVAL_SECONDS", "120")))
        signer_retry = max(60, int(os.getenv("WITHDRAWAL_RECONCILE_SIGNER_RETRY_INTERVAL_SECONDS", "300")))
        return submitted, broadcasted, signer_retry

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
        submitted_after_s, broadcasted_after_s, signer_retry_after_s = self._reconcile_intervals()
        for w in wallet_service.unresolved_withdrawals_for_reconcile(limit=100, submitted_after_s=submitted_after_s, broadcasted_after_s=broadcasted_after_s, signer_retry_after_s=signer_retry_after_s):
            processed += self._reconcile_single(wallet_service, w)
        return processed

    def process_pending_withdrawals(self, wallet_service) -> int:
        return self.process_withdrawals(wallet_service)

    def _execute_single(self, wallet_service, w: dict) -> int:
        try:
            idem = str(w.get("idempotency_key") or "").strip()
            if not idem:
                raise RuntimeError("missing withdrawal idempotency_key")
            wallet_service.persist_withdrawal_idempotency(int(w["id"]), idem)
            request = WithdrawalExecutionRequest(
                withdrawal_id=int(w["id"]), user_id=int(w["user_id"]), asset=str(w["asset"]).upper().strip(),
                amount=str(Decimal(str(w["amount"]))), destination_address=str(w["destination_address"]), idempotency_key=idem,
            )
            if hasattr(self.provider, "execute_withdrawal"):
                result = self.provider.execute_withdrawal(request)
                status, txid = self._map_result(result)
                result_asset = str(getattr(result, "asset", "") or "").strip().upper()
                if result_asset and result_asset != request.asset:
                    raise RetryableSignerError("provider response asset mismatch")
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
                wallet_service.mark_withdrawal_signer_retry(int(w["id"]), sanitize_runtime_error(exc))
            elif isinstance(exc, DeterministicSigningError):
                wallet_service.mark_withdrawal_failed(int(w["id"]), sanitize_runtime_error(exc))
            else:
                wallet_service.mark_withdrawal_signer_retry(int(w["id"]), sanitize_runtime_error(exc))
            return 0

    def _reconcile_single(self, wallet_service, w: dict) -> int:
        if not hasattr(self.provider, "reconcile_withdrawal"):
            return 0
        try:
            req = WithdrawalReconciliationRequest(withdrawal_id=int(w["id"]), idempotency_key=str(w.get("idempotency_key") or ""), provider_ref=str(w.get("provider_ref") or "") or None)
            result = self.provider.reconcile_withdrawal(req)
            result_ref = str(getattr(result, "provider_ref", "") or "").strip()
            stored_ref = str(w.get("provider_ref") or "").strip()
            if stored_ref and result_ref and stored_ref != result_ref:
                # WARNING: reconcile provider_ref drift fails closed to prevent cross-withdrawal rebinding.
                raise RetryableSignerError("provider_ref mismatch during reconcile")
            result_asset = str(getattr(result, "asset", "") or "").strip().upper()
            stored_asset = str(w.get("asset") or "").strip().upper()
            if result_asset and stored_asset and result_asset != stored_asset:
                raise RetryableSignerError("provider response asset mismatch during reconcile")
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
            wallet_service.mark_withdrawal_signer_retry(int(w["id"]), sanitize_runtime_error(exc))
            wallet_service.mark_withdrawal_reconciled(int(w["id"]))
            return 0
