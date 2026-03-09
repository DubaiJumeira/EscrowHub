from __future__ import annotations

import hashlib
import importlib
import logging
import os
from dataclasses import dataclass

from config.settings import Settings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DerivedKey:
    path: str
    private_key_hex: str
    public_address: str


@dataclass(frozen=True)
class DerivedAddress:
    path: str
    public_address: str


class HDWalletDeriver:
    """Single source of truth for deterministic HD derivation."""

    def __init__(self) -> None:
        self.app_env = os.getenv("APP_ENV", "dev").lower()
        self.seed_hex = os.getenv("HD_WALLET_SEED_HEX", "")

    def _require_seed(self) -> str:
        if not self.seed_hex:
            raise RuntimeError("HD_WALLET_SEED_HEX is missing")
        return self.seed_hex

    def _allow_fallback(self) -> bool:
        if self.app_env == "production":
            raise RuntimeError("fallback derivation is disabled in production")
        if not Settings.allow_fallback_derivation:
            raise RuntimeError("fallback derivation disabled; set ALLOW_FALLBACK_DERIVATION=true for non-production")
        LOGGER.warning("Using fallback HD derivation path in non-production")
        return True

    def _require_hdwallet(self):
        try:
            return importlib.import_module("hdwallet")
        except Exception as exc:
            if self.app_env == "production":
                raise RuntimeError("hdwallet library missing") from exc
            return None

    def _derive_fallback_address(self, path: str, prefix: str) -> DerivedAddress:
        self._allow_fallback()
        seed = self._require_seed()
        digest = hashlib.sha256(f"{seed}:{path}".encode()).hexdigest()
        if prefix == "0x":
            address = "0x" + digest[:40]
        elif prefix == "bc1q":
            address = "bc1q" + digest[:38]
        elif prefix == "ltc1q":
            address = "ltc1q" + digest[:37]
        else:
            address = f"{prefix}_{digest[:40]}"
        return DerivedAddress(path=path, public_address=address)

    def _derive_hdwallet_address(self, symbol: str, path: str) -> DerivedAddress:
        lib = self._require_hdwallet()
        if lib is None:
            prefix = {"BTC": "bc1q", "LTC": "ltc1q", "ETH": "0x"}[symbol]
            return self._derive_fallback_address(path, prefix)

        seed_hex = self._require_seed()
        try:
            HDWallet = getattr(lib, "HDWallet")
            cryptocurrencies = importlib.import_module("hdwallet.cryptocurrencies")
            if symbol == "BTC":
                coin_cls = getattr(cryptocurrencies, "BitcoinMainnet")
            elif symbol == "LTC":
                coin_cls = getattr(cryptocurrencies, "LitecoinMainnet")
            else:
                coin_cls = getattr(cryptocurrencies, "EthereumMainnet")

            wallet = HDWallet(cryptocurrency=coin_cls)
            if hasattr(wallet, "from_seed"):
                wallet.from_seed(seed=seed_hex)
            elif hasattr(wallet, "from_seed_hex"):
                wallet.from_seed_hex(seed_hex=seed_hex)
            else:
                raise RuntimeError("unsupported hdwallet seed API")

            if hasattr(wallet, "from_path"):
                wallet.from_path(path=path)
            elif hasattr(wallet, "from_derivation"):
                wallet.from_derivation(path=path)
            else:
                raise RuntimeError("unsupported hdwallet derivation API")

            if symbol in {"BTC", "LTC"}:
                addr = str(wallet.p2wpkh_address()) if hasattr(wallet, "p2wpkh_address") else str(wallet.address())
            else:
                addr = str(wallet.address())
            return DerivedAddress(path=path, public_address=addr)
        except Exception as exc:
            if self.app_env == "production":
                raise RuntimeError(f"hdwallet derivation failed for {symbol}") from exc
            prefix = {"BTC": "bc1q", "LTC": "ltc1q", "ETH": "0x"}[symbol]
            return self._derive_fallback_address(path, prefix)

    def derive_btc(self, user_id: int) -> DerivedKey:
        # WARNING: Private-key derivation is only for non-production signer flows.
        if self.app_env == "production":
            raise RuntimeError("private-key derivation is disabled in production; use external signer")
        path = f"m/84'/0'/{user_id}'/0/0"
        addr = self._derive_hdwallet_address("BTC", path)
        priv = hashlib.sha256(f"priv:{self._require_seed()}:{path}".encode()).hexdigest()
        return DerivedKey(path=path, private_key_hex=priv, public_address=addr.public_address)

    def derive_ltc(self, user_id: int) -> DerivedKey:
        # WARNING: Private-key derivation is only for non-production signer flows.
        if self.app_env == "production":
            raise RuntimeError("private-key derivation is disabled in production; use external signer")
        path = f"m/84'/2'/{user_id}'/0/0"
        addr = self._derive_hdwallet_address("LTC", path)
        priv = hashlib.sha256(f"priv:{self._require_seed()}:{path}".encode()).hexdigest()
        return DerivedKey(path=path, private_key_hex=priv, public_address=addr.public_address)

    def derive_eth(self, user_id: int) -> DerivedKey:
        # WARNING: Private-key derivation is only for non-production signer flows.
        if self.app_env == "production":
            raise RuntimeError("private-key derivation is disabled in production; use external signer")
        path = f"m/44'/60'/{user_id}'/0/0"
        addr = self._derive_hdwallet_address("ETH", path)
        priv = hashlib.sha256(f"priv:{self._require_seed()}:{path}".encode()).hexdigest()
        return DerivedKey(path=path, private_key_hex=priv, public_address=addr.public_address)

    def derive_btc_address(self, user_id: int) -> DerivedAddress:
        path = f"m/84'/0'/{user_id}'/0/0"
        return self._derive_hdwallet_address("BTC", path)

    def derive_ltc_address(self, user_id: int) -> DerivedAddress:
        path = f"m/84'/2'/{user_id}'/0/0"
        return self._derive_hdwallet_address("LTC", path)

    def derive_eth_address(self, user_id: int) -> DerivedAddress:
        path = f"m/44'/60'/{user_id}'/0/0"
        return self._derive_hdwallet_address("ETH", path)

    def derive_sol(self, user_id: int) -> DerivedKey:
        raise RuntimeError("SOL derivation disabled")
