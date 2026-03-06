from __future__ import annotations

import logging
import os

import json
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


class SignerProvider:
    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str) -> str:
        raise NotImplementedError


class VaultSignerProvider(SignerProvider):
    """Vault-backed signer boundary. Signing key material must stay in Vault/HSM."""

    def __init__(self) -> None:
        self.addr = os.getenv("VAULT_ADDR", "")
        self.token = os.getenv("VAULT_TOKEN", "")
        self.path = os.getenv("VAULT_SIGN_PATH", "transit/sign/escrowhub")

    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str) -> str:
        if not self.addr or not self.token:
            raise RuntimeError("Vault signer configuration missing")
        payload = {"input": f"{asset}:{destination_address}:{amount}"}
        req = Request(f"{self.addr}/v1/{self.path}", method="POST", headers={"X-Vault-Token": self.token, "Content-Type": "application/json"}, data=json.dumps(payload).encode())
        with urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        signature = body["data"]["signature"]
        txid = f"vault_tx_{abs(hash(signature))}"
        LOGGER.info("Signed withdrawal for %s to %s", asset, destination_address)
        return txid


class MockSignerProvider(SignerProvider):
    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str) -> str:
        return f"mock_{asset.lower()}_{destination_address[-6:]}_{amount}"


class SignerService:
    def __init__(self) -> None:
        provider = os.getenv("SIGNER_PROVIDER", "mock")
        self.provider: SignerProvider = VaultSignerProvider() if provider == "vault" else MockSignerProvider()

    def process_pending_withdrawals(self, wallet_service) -> int:
        processed = 0
        for w in wallet_service.pending_withdrawals():
            txid = self.provider.sign_and_broadcast(w["asset"], w["destination_address"], str(w["amount"]))
            wallet_service.mark_withdrawal_broadcasted(w["id"], txid)
            processed += 1
        return processed
