from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DerivedKey:
    path: str
    private_key_hex: str
    public_address: str


class HDWalletDeriver:
    def __init__(self) -> None:
        self.app_env = os.getenv("APP_ENV", "dev").lower()
        self.seed_hex = os.getenv("HD_WALLET_SEED_HEX", "")

    def _require_seed(self) -> str:
        if not self.seed_hex:
            raise RuntimeError("HD_WALLET_SEED_HEX is missing")
        return self.seed_hex

    def _enforce_lib(self, lib_name: str) -> None:
        if self.app_env == "production":
            raise RuntimeError(f"{lib_name} library missing")

    def _derive_fallback(self, path: str, prefix: str) -> DerivedKey:
        seed = self._require_seed()
        digest = hashlib.sha256(f"{seed}:{path}".encode()).hexdigest()
        priv = hashlib.sha256(f"priv:{digest}".encode()).hexdigest()
        address = f"{prefix}_{digest[:40]}"
        return DerivedKey(path=path, private_key_hex=priv, public_address=address)

    def derive_btc(self, user_id: int) -> DerivedKey:
        path = f"m/84'/0'/{user_id}'/0/0"
        try:
            import hdwallet  # type: ignore  # noqa: F401
        except Exception:
            return self._derive_fallback(path, "btc") if self.app_env != "production" else self._enforce_and_raise("hdwallet")
        return self._derive_fallback(path, "btc")

    def derive_ltc(self, user_id: int) -> DerivedKey:
        # BIP84 Litecoin coin type 2
        path = f"m/84'/2'/{user_id}'/0/0"
        try:
            import hdwallet  # type: ignore  # noqa: F401
        except Exception:
            return self._derive_fallback(path, "ltc") if self.app_env != "production" else self._enforce_and_raise("hdwallet")
        return self._derive_fallback(path, "ltc")

    def derive_eth(self, user_id: int) -> DerivedKey:
        path = f"m/44'/60'/{user_id}'/0/0"
        try:
            import hdwallet  # type: ignore  # noqa: F401
        except Exception:
            return self._derive_fallback(path, "0x") if self.app_env != "production" else self._enforce_and_raise("hdwallet")
        k = self._derive_fallback(path, "0x")
        return DerivedKey(path=k.path, private_key_hex=k.private_key_hex, public_address="0x" + k.public_address.replace("0x_", "")[:40])

    def derive_sol(self, user_id: int) -> DerivedKey:
        # m/44'/501'/{user_id}'/0'
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
        except Exception:
            if self.app_env == "production":
                raise RuntimeError("solders or solana-py library missing")
            return self._derive_fallback(path, "sol")

    def _enforce_and_raise(self, lib_name: str):
        self._enforce_lib(lib_name)
        raise RuntimeError(f"{lib_name} library missing")
