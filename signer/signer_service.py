from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config.settings import Settings
from signer.errors import (
    AmbiguousBroadcastError,
    DeterministicSigningError,
    RetryableSignerError,
    SignerConfigurationError,
)

LOGGER = logging.getLogger(__name__)
SUPPORTED_SIGNING_ASSETS = {"BTC", "LTC", "ETH", "USDT"}


@dataclass(frozen=True)
class WithdrawalExecutionRequest:
    withdrawal_id: int
    user_id: int
    asset: str
    amount: str
    destination_address: str
    idempotency_key: str


@dataclass(frozen=True)
class WithdrawalExecutionResult:
    status: str
    txid: str | None = None
    provider_ref: str | None = None
    message: str | None = None


class SignerProvider:
    def is_ready(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    def execute_withdrawal(self, req: WithdrawalExecutionRequest) -> WithdrawalExecutionResult:
        raise NotImplementedError

    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        req = WithdrawalExecutionRequest(withdrawal_id=0, user_id=int(user_id or 0), asset=asset, amount=amount, destination_address=destination_address, idempotency_key="legacy")
        result = self.execute_withdrawal(req)
        if (result.status or "").lower().strip() in {"submitted", "broadcasted", "confirmed"} and (result.txid or "").strip():
            return str(result.txid).strip()
        raise RetryableSignerError("legacy signer path did not return broadcasted txid")


class VaultSignerProvider(SignerProvider):
    def __init__(self) -> None:
        self.addr = os.getenv("VAULT_ADDR", "").strip().rstrip("/")
        self.token = os.getenv("VAULT_TOKEN", "").strip()
        self.path = os.getenv("VAULT_SIGN_PATH", "transit/sign/escrowhub").strip().lstrip("/")

    def is_ready(self) -> tuple[bool, str | None]:
        if not self.addr:
            return False, "VAULT_ADDR is missing"
        if Settings.is_production and not self.addr.startswith("https://"):
            return False, "VAULT_ADDR must use https:// in production"
        if not self.token:
            return False, "VAULT_TOKEN is missing"
        return True, None

    def execute_withdrawal(self, req: WithdrawalExecutionRequest) -> WithdrawalExecutionResult:
        symbol = req.asset.upper().strip()
        if symbol not in SUPPORTED_SIGNING_ASSETS:
            raise DeterministicSigningError(f"unsupported signing asset: {symbol}")
        ready, err = self.is_ready()
        if not ready:
            raise SignerConfigurationError(err or "vault signer not ready")

        payload = {
            "idempotency_key": req.idempotency_key,
            "withdrawal_id": int(req.withdrawal_id),
            "user_id": int(req.user_id),
            "asset": symbol,
            "destination_address": req.destination_address,
            "amount": str(req.amount),
        }
        request = Request(
            f"{self.addr}/v1/{self.path}",
            method="POST",
            headers={"X-Vault-Token": self.token, "Content-Type": "application/json"},
            data=json.dumps(payload, sort_keys=True).encode(),
        )
        try:
            with urlopen(request, timeout=15) as resp:
                raw = resp.read().decode() or "{}"
                body = json.loads(raw)
                if int(getattr(resp, "status", 200)) >= 400:
                    raise RetryableSignerError(f"vault signer http status={getattr(resp, 'status', 'unknown')}")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RetryableSignerError("vault signer request failed") from exc
        except ValueError as exc:
            raise RetryableSignerError("vault signer returned malformed JSON") from exc

        if not isinstance(body, dict):
            raise RetryableSignerError("vault signer response malformed")
        # WARNING: Transit-only signing cannot produce a chain-valid tx payload + broadcast txid in this code path.
        # Secure alternative: integrate a dedicated chain-aware signing+broadcast service that returns explicit statuses/txid.
        raise DeterministicSigningError("incomplete signer path: no real transaction builder+broadcaster integration")


class DisabledSignerProvider(SignerProvider):
    def is_ready(self) -> tuple[bool, str | None]:
        return False, "signing provider disabled"

    def execute_withdrawal(self, req: WithdrawalExecutionRequest) -> WithdrawalExecutionResult:
        # WARNING: Fake/non-chain signing is disabled to prevent fabricated withdrawals.
        raise SignerConfigurationError("signing provider disabled: configure a real chain-aware signer+broadcaster")


class SignerService:
    def __init__(self) -> None:
        provider = os.getenv("SIGNER_PROVIDER", "vault").lower().strip()
        if provider == "vault":
            self.provider: SignerProvider = VaultSignerProvider()
        else:
            if Settings.is_production:
                raise RuntimeError("Only real external signer providers are allowed in production")
            self.provider = DisabledSignerProvider()

    def readiness(self) -> tuple[bool, str | None]:
        if not Settings.withdrawals_enabled:
            return False, "WITHDRAWALS_ENABLED=false"
        if hasattr(self.provider, "is_ready"):
            return self.provider.is_ready()
        return True, None

    def _idempotency_key(self, withdrawal_id: int, user_id: int, asset: str, amount: str, destination: str) -> str:
        payload = f"{withdrawal_id}|{user_id}|{asset}|{amount}|{destination}".encode()
        digest = hashlib.sha256(payload).hexdigest()
        return f"wd:{withdrawal_id}:{digest[:24]}"

    def _map_result(self, result: WithdrawalExecutionResult) -> tuple[str, str | None]:
        status = (result.status or "").strip().lower()
        message = (result.message or "").strip()[:500]
        if status in {"submitted", "broadcasted", "confirmed"}:
            if not (result.txid or "").strip():
                raise RetryableSignerError("provider returned broadcast-like status without txid")
            return "broadcasted", str(result.txid).strip()
        if status in {"rejected", "permanent_failure"}:
            raise DeterministicSigningError(message or "withdrawal rejected by provider")
        if status in {"retryable", "ambiguous"}:
            raise RetryableSignerError(message or "withdrawal outcome ambiguous")
        # WARNING: Unknown provider status is treated as ambiguous to prevent unsafe balance release.
        raise RetryableSignerError(f"unknown provider status: {status or 'empty'}")

    def process_pending_withdrawals(self, wallet_service) -> int:
        ready, reason = self.readiness()
        if not ready:
            LOGGER.info("withdrawal processing skipped: %s", reason or "not ready")
            return 0

        processed = 0
        for w in wallet_service.pending_withdrawals():
            try:
                request = WithdrawalExecutionRequest(
                    withdrawal_id=int(w["id"]),
                    user_id=int(w["user_id"]),
                    asset=str(w["asset"]).upper().strip(),
                    amount=str(Decimal(str(w["amount"]))),
                    destination_address=str(w["destination_address"]),
                    idempotency_key=self._idempotency_key(int(w["id"]), int(w["user_id"]), str(w["asset"]), str(w["amount"]), str(w["destination_address"])),
                )
                if hasattr(self.provider, "execute_withdrawal"):
                    result = self.provider.execute_withdrawal(request)
                else:
                    txid = self.provider.sign_and_broadcast(request.asset, request.destination_address, request.amount, user_id=request.user_id)
                    result = WithdrawalExecutionResult(status="broadcasted", txid=str(txid))
                internal_status, txid = self._map_result(result)
                if internal_status == "broadcasted" and txid:
                    wallet_service.mark_withdrawal_broadcasted(int(w["id"]), txid)
                    processed += 1
            except Exception as exc:
                LOGGER.exception("withdrawal processing failed id=%s", w["id"])
                if isinstance(exc, (AmbiguousBroadcastError, RetryableSignerError)):
                    wallet_service.mark_withdrawal_signer_retry(int(w["id"]), str(exc))
                elif isinstance(exc, DeterministicSigningError):
                    wallet_service.mark_withdrawal_failed(int(w["id"]), str(exc))
                else:
                    # WARNING: Unclassified signer errors are treated as ambiguous to prevent accidental fund release.
                    # Secure alternative: map provider failures to explicit typed exceptions and reconcile before any release path.
                    wallet_service.mark_withdrawal_signer_retry(int(w["id"]), str(exc))
        return processed
