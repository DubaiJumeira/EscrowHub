from __future__ import annotations

import importlib
import os
from dataclasses import dataclass


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
    """Deterministic address derivation via standards-compliant BIP44/BIP84 libraries."""

    def __init__(self) -> None:
        self.app_env = os.getenv("APP_ENV", "dev").lower()
        self.seed_hex = os.getenv("HD_WALLET_SEED_HEX", "").strip()

    def _require_seed(self) -> str:
        if not self.seed_hex:
            raise RuntimeError("HD_WALLET_SEED_HEX is missing")
        return self.seed_hex

    def _bip_utils(self):
        try:
            return importlib.import_module("bip_utils")
        except Exception as exc:
            raise RuntimeError("bip_utils is required for standards-compliant wallet derivation") from exc

    def derive_btc(self, user_id: int) -> DerivedKey:
        # WARNING: Runtime private-key derivation is intentionally disabled. Use an external signer/HSM for production-grade signing.
        raise RuntimeError("private-key derivation is disabled; use external signer")

    def derive_ltc(self, user_id: int) -> DerivedKey:
        # WARNING: Runtime private-key derivation is intentionally disabled. Use an external signer/HSM for production-grade signing.
        raise RuntimeError("private-key derivation is disabled; use external signer")

    def derive_eth(self, user_id: int) -> DerivedKey:
        # WARNING: Runtime private-key derivation is intentionally disabled. Use an external signer/HSM for production-grade signing.
        raise RuntimeError("private-key derivation is disabled; use external signer")

    def derive_btc_address(self, user_id: int) -> DerivedAddress:
        b = self._bip_utils()
        seed = bytes.fromhex(self._require_seed())
        path = f"m/84'/0'/{int(user_id)}'/0/0"
        ctx = b.Bip84.FromSeed(seed, b.Bip84Coins.BITCOIN).Purpose().Coin().Account(int(user_id)).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(0)
        return DerivedAddress(path=path, public_address=ctx.PublicKey().ToAddress())

    def derive_ltc_address(self, user_id: int) -> DerivedAddress:
        b = self._bip_utils()
        seed = bytes.fromhex(self._require_seed())
        path = f"m/84'/2'/{int(user_id)}'/0/0"
        ctx = b.Bip84.FromSeed(seed, b.Bip84Coins.LITECOIN).Purpose().Coin().Account(int(user_id)).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(0)
        return DerivedAddress(path=path, public_address=ctx.PublicKey().ToAddress())

    def derive_eth_address(self, user_id: int) -> DerivedAddress:
        b = self._bip_utils()
        seed = bytes.fromhex(self._require_seed())
        path = f"m/44'/60'/{int(user_id)}'/0/0"
        ctx = b.Bip44.FromSeed(seed, b.Bip44Coins.ETHEREUM).Purpose().Coin().Account(int(user_id)).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(0)
        return DerivedAddress(path=path, public_address=ctx.PublicKey().ToAddress())
