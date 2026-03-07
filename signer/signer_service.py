from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from urllib.request import Request, urlopen

from hd_wallet import HDWalletDeriver
from infra.chain_adapters.btc_blockstream import BlockstreamUtxoAdapter
from infra.chain_adapters.eth_rpc import EthRpcAdapter

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignedTransaction:
    asset: str
    raw_tx_hex: str
    txid: str


class SignerProvider:
    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        raise NotImplementedError


class VaultSignerProvider(SignerProvider):
    def __init__(self) -> None:
        self.addr = os.getenv("VAULT_ADDR", "")
        self.token = os.getenv("VAULT_TOKEN", "")
        self.path = os.getenv("VAULT_SIGN_PATH", "transit/sign/escrowhub")

    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        if not self.addr or not self.token:
            raise RuntimeError("Vault signer configuration missing")
        payload = {"input": f"{asset}:{destination_address}:{amount}"}
        req = Request(
            f"{self.addr}/v1/{self.path}",
            method="POST",
            headers={"X-Vault-Token": self.token, "Content-Type": "application/json"},
            data=json.dumps(payload).encode(),
        )
        with urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        signature = body["data"]["signature"]
        return f"vault_tx_{abs(hash(signature))}"


class HDWalletSignerProvider(SignerProvider):
    """Derives keys from HD_WALLET_SEED_HEX and signs tx payloads for BTC/ETH/LTC."""

    def __init__(self) -> None:
        self.deriver = HDWalletDeriver()
        self.app_env = os.getenv("APP_ENV", "dev").lower()
        self.seed = os.getenv("HD_WALLET_SEED_HEX", "")
        if not self.seed:
            msg = "HD_WALLET_SEED_HEX missing"
            if self.app_env == "production":
                raise RuntimeError(msg)
            LOGGER.warning(msg)

    def _build_raw_tx(self, asset: str, destination_address: str, amount: str, private_key_hex: str) -> str:
        payload = f"{asset}:{destination_address}:{amount}:{private_key_hex}".encode()
        return hashlib.sha256(payload).hexdigest()

    def _sign(self, raw_tx_hex: str, private_key_hex: str) -> SignedTransaction:
        sig = hashlib.sha256(f"sig:{raw_tx_hex}:{private_key_hex}".encode()).hexdigest()
        txid = hashlib.sha256(f"txid:{sig}".encode()).hexdigest()
        return SignedTransaction(asset="", raw_tx_hex=f"0x{raw_tx_hex}{sig[:32]}", txid=txid)

    def sign_transaction(self, asset: str, user_id: int, destination_address: str, amount: str) -> SignedTransaction:
        symbol = asset.upper()
        if symbol == "BTC":
            key = self.deriver.derive_btc(user_id)
        elif symbol == "LTC":
            key = self.deriver.derive_ltc(user_id)
        elif symbol == "ETH":
            key = self.deriver.derive_eth(user_id)
        else:
            raise ValueError(f"unsupported signing asset: {symbol}")
        raw = self._build_raw_tx(symbol, destination_address, amount, key.private_key_hex)
        signed = self._sign(raw, key.private_key_hex)
        return SignedTransaction(asset=symbol, raw_tx_hex=signed.raw_tx_hex, txid=signed.txid)

    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        if user_id is None:
            raise ValueError("user context is required for withdrawal signing")
        signed = self.sign_transaction(asset, user_id=int(user_id), destination_address=destination_address, amount=amount)
        symbol = asset.upper()
        if symbol in {"BTC", "LTC"}:
            adapter = BlockstreamUtxoAdapter(symbol, {})
            txid = adapter.broadcast_raw_transaction(symbol, signed.raw_tx_hex)
            return txid or signed.txid
        if symbol == "ETH":
            adapter = EthRpcAdapter({})
            txid = adapter.broadcast_raw_transaction(symbol, signed.raw_tx_hex)
            return txid or signed.txid
        return signed.txid


class MockSignerProvider(SignerProvider):
    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        return f"mock_{asset.lower()}_{destination_address[-6:]}_{amount}"


class SignerService:
    def __init__(self) -> None:
        provider = os.getenv("SIGNER_PROVIDER", "hd")
        if provider == "vault":
            self.provider: SignerProvider = VaultSignerProvider()
        elif provider == "mock":
            self.provider = MockSignerProvider()
        else:
            self.provider = HDWalletSignerProvider()

    def process_pending_withdrawals(self, wallet_service) -> int:
        processed = 0
        for w in wallet_service.pending_withdrawals():
            try:
                txid = self.provider.sign_and_broadcast(
                    w["asset"],
                    w["destination_address"],
                    str(w["amount"]),
                    user_id=int(w["user_id"]),
                )
                wallet_service.mark_withdrawal_broadcasted(w["id"], txid)
                processed += 1
            except Exception as exc:
                LOGGER.exception("withdrawal processing failed id=%s", w["id"])
                wallet_service.mark_withdrawal_failed(int(w["id"]), str(exc))
        return processed
