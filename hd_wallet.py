from __future__ import annotations

import hashlib
import importlib
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DerivedKey:
    path: str
    private_key_hex: str
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

    def _require_hdwallet(self):
        try:
            return importlib.import_module("hdwallet")
        except Exception as exc:
            if self.app_env == "production":
                raise RuntimeError("hdwallet library missing") from exc
            return None

    def _derive_fallback(self, path: str, prefix: str) -> DerivedKey:
        # Non-production deterministic fallback only.
        seed = self._require_seed()
        digest = hashlib.sha256(f"{seed}:{path}".encode()).hexdigest()
        priv = hashlib.sha256(f"priv:{digest}".encode()).hexdigest()
        if prefix == "0x":
            address = "0x" + digest[:40]
        elif prefix == "bc1q":
            address = "bc1q" + digest[:38]
        elif prefix == "ltc1q":
            address = "ltc1q" + digest[:37]
        else:
            address = f"{prefix}_{digest[:40]}"
        return DerivedKey(path=path, private_key_hex=priv, public_address=address)

    def _derive_hdwallet_address(self, symbol: str, path: str) -> DerivedKey:
        lib = self._require_hdwallet()
        if lib is None:
            prefix = {"BTC": "bc1q", "LTC": "ltc1q", "ETH": "0x"}[symbol]
            return self._derive_fallback(path, prefix)

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
            # common API pattern
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

            priv = ""
            if hasattr(wallet, "private_key"):
                priv = str(wallet.private_key())
            elif hasattr(wallet, "wif"):
                priv = str(wallet.wif())

            if symbol == "BTC":
                addr = str(wallet.p2wpkh_address()) if hasattr(wallet, "p2wpkh_address") else str(wallet.address())
            elif symbol == "LTC":
                addr = str(wallet.p2wpkh_address()) if hasattr(wallet, "p2wpkh_address") else str(wallet.address())
            else:
                addr = str(wallet.address())
            return DerivedKey(path=path, private_key_hex=priv, public_address=addr)
        except Exception as exc:
            if self.app_env == "production":
                raise RuntimeError(f"hdwallet derivation failed for {symbol}") from exc
            prefix = {"BTC": "bc1q", "LTC": "ltc1q", "ETH": "0x"}[symbol]
            return self._derive_fallback(path, prefix)

    def derive_btc(self, user_id: int) -> DerivedKey:
        path = f"m/84'/0'/{user_id}'/0/0"
        return self._derive_hdwallet_address("BTC", path)

    def derive_ltc(self, user_id: int) -> DerivedKey:
        path = f"m/84'/2'/{user_id}'/0/0"
        return self._derive_hdwallet_address("LTC", path)

    def derive_eth(self, user_id: int) -> DerivedKey:
        path = f"m/44'/60'/{user_id}'/0/0"
        return self._derive_hdwallet_address("ETH", path)

    def derive_sol(self, user_id: int) -> DerivedKey:
        path = f"m/44'/501'/{user_id}'/0'"
        try:
            from solders.keypair import Keypair  # type: ignore

            seed = hashlib.sha256(f"{self._require_seed()}:{path}".encode()).digest()
            kp = Keypair.from_seed(seed[:32])
            return DerivedKey(path=path, private_key_hex=seed.hex(), public_address=str(kp.pubkey()))
        except Exception:
            pass

        try:
            from solana.keypair import Keypair  # type: ignore

            seed = hashlib.sha256(f"{self._require_seed()}:{path}".encode()).digest()
            kp = Keypair.from_seed(seed[:32])
            return DerivedKey(path=path, private_key_hex=seed.hex(), public_address=str(kp.public_key))
        except Exception as exc:
            if self.app_env == "production":
                raise RuntimeError("SOL derivation requires solders or solana-py") from exc
            return self._derive_fallback(path, "sol")
