from __future__ import annotations

import importlib
import os
from dataclasses import dataclass

from config.settings import Settings


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

    def _require_xpub(self, asset: str) -> str:
        key_map = {
            "BTC": Settings.btc_xpub,
            "LTC": Settings.ltc_xpub,
            "ETH": Settings.eth_xpub,
            "USDT": Settings.eth_xpub,
        }
        xpub = (key_map.get(asset.upper()) or "").strip()
        if xpub:
            return xpub
        provider_mode = os.getenv("ADDRESS_PROVIDER", "auto").strip().lower()
        if Settings.is_production and provider_mode not in {"local_hd", "local", "seed", "auto", "", "default"}:
            # WARNING: Production deposit routing normally prefers xpub/address-service only.
            # Self-hosted single-node deployments may explicitly opt into local_hd seed-based issuance.
            raise RuntimeError(f"No valid production derivation mode configured for {asset.upper()}; configure an external derivation service/HSM-backed address service.")
        return ""

    def validate_xpub_configuration(self) -> None:
        configured = {"BTC": Settings.btc_xpub, "LTC": Settings.ltc_xpub, "ETH": Settings.eth_xpub}
        if any((v or "").strip() for v in configured.values()):
            # WARNING: Current stored derivation contract uses hardened user account nodes (m/.../{user_id}'/...),
            # which cannot be derived from public extended keys. Fail closed to avoid misrouting deposits.
            # Secure alternative: use an external HSM/address-derivation service, or migrate explicitly to an xpub-compatible non-hardened user index path.
            raise RuntimeError(
                "xpub configuration is incompatible with current derivation path contract "
                "(m/.../{user_id}'/... requires hardened derivation). "
                "Disable xpub mode and use approved external derivation service or perform a planned migration."
            )

    def _bip_utils(self):
        try:
            return importlib.import_module("bip_utils")
        except Exception as exc:
            raise RuntimeError("bip_utils is required for standards-compliant wallet derivation") from exc

    def _user_account_and_index(self, user_id: int) -> tuple[int, int]:
        raw = int(user_id)
        if raw < 0:
            raise ValueError("user_id must be non-negative")
        radix = 0x80000000  # BIP32 hardened boundary
        account = raw // radix
        index = raw % radix
        if account >= radix:
            raise ValueError(f"user_id too large for deterministic derivation mapping: {raw}")
        return account, index

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
        self.validate_xpub_configuration()
        b = self._bip_utils()
        account, index = self._user_account_and_index(user_id)
        path = f"m/84'/0'/{account}'/0/{index}"
        xpub = self._require_xpub("BTC")
        if xpub:
            ctx = b.Bip84.FromExtendedKey(xpub, b.Bip84Coins.BITCOIN).Purpose().Coin().Account(account).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(index)
        else:
            seed = bytes.fromhex(self._require_seed())
            ctx = b.Bip84.FromSeed(seed, b.Bip84Coins.BITCOIN).Purpose().Coin().Account(account).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(index)
        return DerivedAddress(path=path, public_address=ctx.PublicKey().ToAddress())

    def derive_ltc_address(self, user_id: int) -> DerivedAddress:
        self.validate_xpub_configuration()
        b = self._bip_utils()
        account, index = self._user_account_and_index(user_id)
        path = f"m/84'/2'/{account}'/0/{index}"
        xpub = self._require_xpub("LTC")
        if xpub:
            ctx = b.Bip84.FromExtendedKey(xpub, b.Bip84Coins.LITECOIN).Purpose().Coin().Account(account).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(index)
        else:
            seed = bytes.fromhex(self._require_seed())
            ctx = b.Bip84.FromSeed(seed, b.Bip84Coins.LITECOIN).Purpose().Coin().Account(account).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(index)
        return DerivedAddress(path=path, public_address=ctx.PublicKey().ToAddress())

    def derive_eth_address(self, user_id: int) -> DerivedAddress:
        self.validate_xpub_configuration()
        b = self._bip_utils()
        account, index = self._user_account_and_index(user_id)
        path = f"m/44'/60'/{account}'/0/{index}"
        xpub = self._require_xpub("ETH")
        if xpub:
            ctx = b.Bip44.FromExtendedKey(xpub, b.Bip44Coins.ETHEREUM).Purpose().Coin().Account(account).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(index)
        else:
            seed = bytes.fromhex(self._require_seed())
            ctx = b.Bip44.FromSeed(seed, b.Bip44Coins.ETHEREUM).Purpose().Coin().Account(account).Change(b.Bip44Changes.CHAIN_EXT).AddressIndex(index)
        return DerivedAddress(path=path, public_address=ctx.PublicKey().ToAddress())
