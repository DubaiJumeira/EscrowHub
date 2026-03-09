from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config.settings import Settings
from signer.errors import RetryableSignerError, SignerConfigurationError

SUPPORTED_WITHDRAWAL_ASSETS = {"BTC", "LTC", "ETH", "USDT"}


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
    asset: str | None = None
    provider_ref: str | None = None
    external_status: str | None = None
    message: str | None = None
    submitted_at: str | None = None
    broadcasted_at: str | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class WithdrawalReconciliationRequest:
    withdrawal_id: int
    idempotency_key: str
    provider_ref: str | None = None


@dataclass(frozen=True)
class WithdrawalReconciliationResult:
    status: str
    txid: str | None = None
    asset: str | None = None
    provider_ref: str | None = None
    external_status: str | None = None
    message: str | None = None
    submitted_at: str | None = None
    broadcasted_at: str | None = None
    metadata: dict | None = None


class WithdrawalProvider:
    provider_origin = "unknown"

    def is_ready(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    def execute_withdrawal(self, req: WithdrawalExecutionRequest) -> WithdrawalExecutionResult:
        raise NotImplementedError

    def reconcile_withdrawal(self, req: WithdrawalReconciliationRequest) -> WithdrawalReconciliationResult:
        raise NotImplementedError


class HttpWithdrawalProvider(WithdrawalProvider):
    provider_origin = "external_http"

    def __init__(self) -> None:
        self.base_url = os.getenv("WITHDRAWAL_PROVIDER_URL", "").strip().rstrip("/")
        self.auth_token = os.getenv("WITHDRAWAL_PROVIDER_TOKEN", "").strip()
        self.timeout_seconds = max(3, min(30, int(os.getenv("WITHDRAWAL_PROVIDER_TIMEOUT_SECONDS", "10"))))

    def is_ready(self) -> tuple[bool, str | None]:
        if not self.base_url:
            return False, "WITHDRAWAL_PROVIDER_URL is missing"
        if Settings.is_production and not self.base_url.startswith("https://"):
            return False, "WITHDRAWAL_PROVIDER_URL must use https:// in production"
        if Settings.is_production and not self.auth_token:
            return False, "WITHDRAWAL_PROVIDER_TOKEN is required in production"
        try:
            body = self._request_json("GET", "/health", None)
            assets = set(body.get("supported_assets") or [])
            if not SUPPORTED_WITHDRAWAL_ASSETS.issubset(assets):
                return False, "healthcheck missing supported_assets coverage"
        except Exception as exc:
            return False, f"healthcheck failed: {exc}"
        return True, None

    def execute_withdrawal(self, req: WithdrawalExecutionRequest) -> WithdrawalExecutionResult:
        payload = {
            "withdrawal_id": int(req.withdrawal_id),
            "user_id": int(req.user_id),
            "asset": req.asset,
            "amount": req.amount,
            "destination_address": req.destination_address,
            "idempotency_key": req.idempotency_key,
        }
        body = self._request_json("POST", "/v1/withdrawals", payload)
        return self._parse_result(body, allow_missing_ref=False)

    def reconcile_withdrawal(self, req: WithdrawalReconciliationRequest) -> WithdrawalReconciliationResult:
        payload = {
            "withdrawal_id": int(req.withdrawal_id),
            "idempotency_key": req.idempotency_key,
            "provider_ref": req.provider_ref,
        }
        body = self._request_json("POST", "/v1/withdrawals/reconcile", payload)
        parsed = self._parse_result(body, allow_missing_ref=True)
        return WithdrawalReconciliationResult(**parsed.__dict__)

    def _request_json(self, method: str, path: str, payload: dict | None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        req = Request(f"{self.base_url}{path}", method=method, headers=headers)
        if payload is not None:
            req.data = json.dumps(payload, sort_keys=True).encode()
        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = (resp.read() or b"{}").decode()
                if int(getattr(resp, "status", 200)) >= 400:
                    raise RetryableSignerError(f"withdrawal provider http status={getattr(resp, 'status', 'unknown')}")
                body = json.loads(raw)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RetryableSignerError("withdrawal provider request failed") from exc
        except ValueError as exc:
            # WARNING: Malformed provider payloads fail closed to signer_retry to prevent unsafe balance release.
            raise RetryableSignerError("withdrawal provider returned malformed JSON") from exc
        if not isinstance(body, dict):
            raise RetryableSignerError("withdrawal provider response malformed")
        return body

    def _parse_result(self, body: dict, allow_missing_ref: bool) -> WithdrawalExecutionResult:
        status = str(body.get("status") or "").strip().lower()
        if not status:
            raise RetryableSignerError("withdrawal provider response missing status")
        txid = str(body.get("txid") or "").strip() or None
        provider_ref = str(body.get("provider_ref") or "").strip() or None
        ext_status = str(body.get("external_status") or status).strip().lower() or None
        asset = str(body.get("asset") or "").strip().upper()
        if asset and asset not in SUPPORTED_WITHDRAWAL_ASSETS:
            raise RetryableSignerError("withdrawal provider returned unsupported asset")
        if status in {"submitted", "broadcasted", "confirmed"} and not provider_ref:
            raise RetryableSignerError("withdrawal provider response missing provider_ref")
        if status in {"broadcasted", "confirmed"} and not txid:
            raise RetryableSignerError("withdrawal provider response missing txid")
        return WithdrawalExecutionResult(
            status=status,
            txid=txid,
            asset=asset or None,
            provider_ref=provider_ref,
            external_status=ext_status,
            message=str(body.get("message") or "").strip()[:500] or None,
            submitted_at=str(body.get("submitted_at") or "").strip() or None,
            broadcasted_at=str(body.get("broadcasted_at") or "").strip() or None,
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
        )


class DisabledWithdrawalProvider(WithdrawalProvider):
    provider_origin = "disabled"

    def is_ready(self) -> tuple[bool, str | None]:
        return False, "withdrawal provider disabled"

    def execute_withdrawal(self, req: WithdrawalExecutionRequest) -> WithdrawalExecutionResult:
        raise SignerConfigurationError("withdrawal provider disabled")

    def reconcile_withdrawal(self, req: WithdrawalReconciliationRequest) -> WithdrawalReconciliationResult:
        raise SignerConfigurationError("withdrawal provider disabled")
