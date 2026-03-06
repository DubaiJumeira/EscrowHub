from __future__ import annotations

import base64
import json
import logging
import os
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


class SignerProvider:
    def sign_eth_transaction(self, tx_digest_hex: str) -> str:
        raise NotImplementedError

    def sign_btc_transaction(self, payload: str) -> str:
        raise NotImplementedError

    def sign_sol_transaction(self, payload: str) -> str:
        raise NotImplementedError

    def sign_xrp_transaction(self, payload: str) -> str:
        raise NotImplementedError


class VaultSignerProvider(SignerProvider):
    def __init__(self) -> None:
        self.addr = os.getenv("VAULT_ADDR", "")
        self.token = os.getenv("VAULT_TOKEN", "")
        self.namespace = os.getenv("VAULT_NAMESPACE", "")
        self.mount = os.getenv("VAULT_TRANSIT_MOUNT", "transit")
        self.eth_key = os.getenv("VAULT_ETH_KEY_NAME", "escrowhub-eth")

    def _sign_digest(self, key_name: str, digest_hex: str) -> str:
        if not self.addr or not self.token:
            raise RuntimeError("Vault signer configuration missing")
        digest_b64 = base64.b64encode(bytes.fromhex(digest_hex.replace("0x", ""))).decode()
        payload = {"input": digest_b64, "key_version": 1, "marshaling_algorithm": "asn1"}
        headers = {"X-Vault-Token": self.token, "Content-Type": "application/json"}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        req = Request(
            f"{self.addr}/v1/{self.mount}/sign/{key_name}",
            method="POST",
            headers=headers,
            data=json.dumps(payload).encode(),
        )
        with urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        return body["data"]["signature"]

    def sign_eth_transaction(self, tx_digest_hex: str) -> str:
        return self._sign_digest(self.eth_key, tx_digest_hex)

    def sign_btc_transaction(self, payload: str) -> str:
        raise NotImplementedError("Vault BTC signing not implemented in v1")

    def sign_sol_transaction(self, payload: str) -> str:
        raise NotImplementedError("Vault SOL signing not implemented in v1")

    def sign_xrp_transaction(self, payload: str) -> str:
        raise NotImplementedError("Vault XRP signing not implemented in v1")


class LocalSignerProvider(SignerProvider):
    def sign_eth_transaction(self, tx_digest_hex: str) -> str:
        return f"local_sig_{tx_digest_hex[-16:]}"

    def sign_btc_transaction(self, payload: str) -> str:
        return f"local_btc_sig_{abs(hash(payload))}"

    def sign_sol_transaction(self, payload: str) -> str:
        return f"local_sol_sig_{abs(hash(payload))}"

    def sign_xrp_transaction(self, payload: str) -> str:
        return f"local_xrp_sig_{abs(hash(payload))}"


class SignerService:
    def __init__(self) -> None:
        mode = os.getenv("SIGNER_MODE", "local")
        if mode not in {"local", "vault"}:
            raise RuntimeError("SIGNER_MODE must be local or vault")
        if os.getenv("APP_ENV", "dev") == "production" and mode != "vault":
            raise RuntimeError("production requires SIGNER_MODE=vault")
        self.provider: SignerProvider = VaultSignerProvider() if mode == "vault" else LocalSignerProvider()

    def _sign_with_asset(self, asset: str, payload: str) -> str:
        symbol = asset.upper()
        if symbol in {"ETH", "USDT", "USDC"}:
            sig = self.provider.sign_eth_transaction(payload)
        elif symbol in {"BTC", "LTC"}:
            sig = self.provider.sign_btc_transaction(payload)
        elif symbol == "SOL":
            sig = self.provider.sign_sol_transaction(payload)
        elif symbol == "XRP":
            sig = self.provider.sign_xrp_transaction(payload)
        else:
            raise ValueError(f"unsupported asset: {asset}")
        return f"{symbol.lower()}_tx_{abs(hash(sig))}"

    def process_pending_withdrawals(self, wallet_service) -> int:
        processed = 0
        for w in wallet_service.pending_withdrawals():
            try:
                payload = f"{w['asset']}:{w['destination_address']}:{w['amount']}"
                txid = self._sign_with_asset(w["asset"], payload)
                wallet_service.mark_withdrawal_broadcasted(w["id"], txid)
                processed += 1
            except Exception:
                LOGGER.exception("withdrawal broadcast failed id=%s", w["id"])
                wallet_service.mark_withdrawal_failed(w["id"])
        return processed
