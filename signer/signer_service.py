from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config.settings import Settings
from hd_wallet import HDWalletDeriver
from infra.chain_adapters.btc_blockstream import BlockstreamUtxoAdapter
from infra.chain_adapters.eth_rpc import EthRpcAdapter

LOGGER = logging.getLogger(__name__)
SUPPORTED_SIGNING_ASSETS = {"BTC", "LTC", "ETH", "USDT"}


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
        symbol = asset.upper()
        if symbol not in SUPPORTED_SIGNING_ASSETS:
            raise ValueError(f"unsupported signing asset: {symbol}")
        if not self.addr or not self.token:
            raise RuntimeError("Vault signer configuration missing")
        payload = {"input": f"{symbol}:{destination_address}:{amount}"}
        req = Request(
            f"{self.addr}/v1/{self.path}",
            method="POST",
            headers={"X-Vault-Token": self.token, "Content-Type": "application/json"},
            data=json.dumps(payload).encode(),
        )
        try:
            with urlopen(req, timeout=15) as resp:
                if resp.status and int(resp.status) >= 400:
                    raise RuntimeError(f"vault signer http status={resp.status}")
                body = json.loads(resp.read().decode() or "{}")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError("vault signer request failed") from exc

        if body.get("errors"):
            raise RuntimeError("vault signer returned errors")
        data = body.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("vault signer response missing data")
        signature = data.get("signature")
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("vault signer response missing signature")

        # WARNING: Vault transit sign API does not broadcast transactions; no real txid is available.
        raise RuntimeError("vault signer cannot broadcast and cannot provide a real txid; use a broadcaster-integrated signer")


class HDWalletSignerProvider(SignerProvider):
    """Non-production helper signer. Not safe for production broadcasting."""

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
        symbol = asset.upper()
        if symbol == "USDT":
            raise ValueError("HD signer does not support USDT")
        if symbol not in {"BTC", "LTC", "ETH"}:
            raise ValueError(f"unsupported signing asset: {symbol}")
        if user_id is None:
            raise ValueError("user context is required for withdrawal signing")
        signed = self.sign_transaction(symbol, user_id=int(user_id), destination_address=destination_address, amount=amount)
        if symbol in {"BTC", "LTC"}:
            adapter = BlockstreamUtxoAdapter(symbol, {})
            txid = adapter.broadcast_raw_transaction(symbol, signed.raw_tx_hex)
        else:
            adapter = EthRpcAdapter({})
            txid = adapter.broadcast_raw_transaction(symbol, signed.raw_tx_hex)
        if not txid:
            raise RuntimeError("broadcast failed: empty txid")
        return txid


class MockSignerProvider(SignerProvider):
    def sign_and_broadcast(self, asset: str, destination_address: str, amount: str, user_id: int | None = None) -> str:
        symbol = asset.upper()
        if symbol not in SUPPORTED_SIGNING_ASSETS:
            raise ValueError(f"unsupported signing asset: {symbol}")
        return f"mock_{symbol.lower()}_{destination_address[-6:]}_{amount}"


class SignerService:
    def __init__(self) -> None:
        provider = os.getenv("SIGNER_PROVIDER", "hd").lower()
        if Settings.is_production and provider == "hd":
            raise RuntimeError("SIGNER_PROVIDER=hd is not allowed in production")
        if provider == "vault":
            self.provider: SignerProvider = VaultSignerProvider()
        elif provider == "mock":
            if Settings.is_production:
                raise RuntimeError("SIGNER_PROVIDER=mock is not allowed in production")
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
                if not isinstance(txid, str) or not txid.strip():
                    raise RuntimeError("signer returned invalid txid")
                wallet_service.mark_withdrawal_broadcasted(w["id"], txid)
                processed += 1
            except Exception as exc:
                LOGGER.exception("withdrawal processing failed id=%s", w["id"])
                wallet_service.mark_withdrawal_failed(int(w["id"]), str(exc))
        return processed
