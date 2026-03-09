from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config.settings import Settings
from signer.errors import AmbiguousBroadcastError, DeterministicSigningError, SignerConfigurationError

LOGGER = logging.getLogger(__name__)
SUPPORTED_SIGNING_ASSETS = {"BTC", "LTC", "ETH", "USDT"}


class SignerProvider:
    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        raise NotImplementedError


class VaultSignerProvider(SignerProvider):
    def __init__(self) -> None:
        self.addr = os.getenv("VAULT_ADDR", "")
        self.token = os.getenv("VAULT_TOKEN", "")
        self.path = os.getenv("VAULT_SIGN_PATH", "transit/sign/escrowhub")

    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        symbol = asset.upper()
        if symbol not in SUPPORTED_SIGNING_ASSETS:
            raise ValueError(f"unsupported signing asset: {symbol}")
        if not self.addr or not self.token:
            raise SignerConfigurationError("Vault signer configuration missing")

        payload = {"input": f"{symbol}:{destination_address}:{amount}"}
        req = Request(
            f"{self.addr}/v1/{self.path}",
            method="POST",
            headers={"X-Vault-Token": self.token, "Content-Type": "application/json"},
            data=json.dumps(payload).encode(),
        )
        try:
            with urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode() or "{}")
                if resp.status and int(resp.status) >= 400:
                    raise RuntimeError(f"vault signer http status={resp.status}")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise AmbiguousBroadcastError("vault signer request failed") from exc

        if body.get("errors"):
            raise DeterministicSigningError("vault signer returned errors")
        # WARNING: Transit-only signing cannot produce a chain-valid tx payload + broadcast txid in this code path.
        raise DeterministicSigningError("incomplete signer path: no real transaction builder+broadcaster integration")


class DisabledSignerProvider(SignerProvider):
    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        # WARNING: Fake/non-chain signing is disabled to prevent fabricated withdrawals.
        raise SignerConfigurationError("signing provider disabled: configure a real chain-aware signer+broadcaster")


class SignerService:
    def __init__(self) -> None:
        provider = os.getenv("SIGNER_PROVIDER", "vault").lower()
        if provider == "vault":
            self.provider: SignerProvider = VaultSignerProvider()
        else:
            if Settings.is_production:
                raise RuntimeError("Only real external signer providers are allowed in production")
            self.provider = DisabledSignerProvider()

    def process_pending_withdrawals(self, wallet_service) -> int:
        if not Settings.withdrawals_enabled:
            LOGGER.info("withdrawal processing skipped: WITHDRAWALS_ENABLED=false")
            return 0
        processed = 0
        for w in wallet_service.pending_withdrawals():
            try:
                txid = self.provider.sign_and_broadcast(
                    w["asset"],
                    w["destination_address"],
                    str(w["amount"]),
                    user_id=int(w["user_id"]),
                )
                if not isinstance(txid, str) or not txid.strip():
                    raise RuntimeError("signer returned invalid txid")
                wallet_service.mark_withdrawal_broadcasted(w["id"], txid)
                processed += 1
            except Exception as exc:
                LOGGER.exception("withdrawal processing failed id=%s", w["id"])
                if isinstance(exc, AmbiguousBroadcastError):
                    wallet_service.mark_withdrawal_signer_retry(int(w["id"]), str(exc))
                elif isinstance(exc, DeterministicSigningError):
                    wallet_service.mark_withdrawal_failed(int(w["id"]), str(exc))
                else:
                    # WARNING: Unclassified signer errors are treated as ambiguous to prevent accidental fund release.
                    # Secure alternative: map provider failures to explicit typed exceptions and reconcile before any release path.
                    wallet_service.mark_withdrawal_signer_retry(int(w["id"]), str(exc))
        return processed
