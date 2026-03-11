from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config.settings import Settings
from hd_wallet import HDWalletDeriver

SUPPORTED_ASSETS = {"BTC", "LTC", "ETH", "USDT"}


@dataclass(frozen=True)
class IssuedAddress:
    address: str
    provider_origin: str
    provider_ref: str
    asset: str | None = None
    chain_family: str | None = None


class AddressProvider:
    def is_ready(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    def get_or_create_address(self, user_id: int, asset: str) -> IssuedAddress:
        raise NotImplementedError


class DisabledAddressProvider(AddressProvider):
    def is_ready(self) -> tuple[bool, str | None]:
        return False, "no approved deposit address provider configured"

    def get_or_create_address(self, user_id: int, asset: str) -> IssuedAddress:
        raise RuntimeError("deposit address provider is disabled")


class LocalHDAddressProvider(AddressProvider):
    """Self-hosted deterministic address issuance for single-node VPS deployments."""

    def __init__(self) -> None:
        self.hd = HDWalletDeriver()

    def _validate(self) -> None:
        seed = os.getenv("HD_WALLET_SEED_HEX", "").strip()
        if not seed:
            raise RuntimeError("HD_WALLET_SEED_HEX is missing")
        try:
            raw = bytes.fromhex(seed)
        except ValueError as exc:
            raise RuntimeError("HD_WALLET_SEED_HEX must be valid hex") from exc
        if len(raw) < 16:
            raise RuntimeError("HD_WALLET_SEED_HEX is too short")
        if Settings.is_production and not Settings.encryption_key.strip():
            raise RuntimeError("ENCRYPTION_KEY is required in production for encrypted wallet route storage")
        self.hd._bip_utils()

    def is_ready(self) -> tuple[bool, str | None]:
        try:
            self._validate()
        except RuntimeError as exc:
            return False, str(exc)
        return True, None

    def get_or_create_address(self, user_id: int, asset: str) -> IssuedAddress:
        self._validate()
        symbol = asset.upper().strip()
        if symbol not in SUPPORTED_ASSETS:
            raise ValueError(f"unsupported asset: {symbol}")
        if symbol == "BTC":
            derived = self.hd.derive_btc_address(int(user_id))
        elif symbol == "LTC":
            derived = self.hd.derive_ltc_address(int(user_id))
        else:
            derived = self.hd.derive_eth_address(int(user_id))
        chain_family = "ETHEREUM" if symbol in {"ETH", "USDT"} else symbol
        return IssuedAddress(
            address=derived.public_address,
            provider_origin="local_hd",
            provider_ref=f"localhd:{symbol}:{int(user_id)}",
            asset=symbol,
            chain_family=chain_family,
        )


class HttpAddressProvider(AddressProvider):
    def __init__(self) -> None:
        self.base_url = os.getenv("ADDRESS_PROVIDER_URL", "").strip().rstrip("/")
        self.token = os.getenv("ADDRESS_PROVIDER_TOKEN", "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _validate_production_config(self) -> None:
        if not Settings.is_production:
            return
        if not self.base_url:
            raise RuntimeError("ADDRESS_PROVIDER_URL is missing")
        if not self.base_url.startswith("https://"):
            raise RuntimeError("ADDRESS_PROVIDER_URL must use https:// in production")
        if not self.token:
            raise RuntimeError("ADDRESS_PROVIDER_TOKEN is required in production")

    def is_ready(self) -> tuple[bool, str | None]:
        try:
            self._validate_production_config()
        except RuntimeError as exc:
            return False, str(exc)
        if not self.base_url:
            return False, "ADDRESS_PROVIDER_URL is missing"
        req = Request(f"{self.base_url}/health", method="GET", headers=self._headers())
        try:
            with urlopen(req, timeout=10) as resp:
                if int(getattr(resp, "status", 200)) >= 400:
                    return False, f"health status={getattr(resp, 'status', 'unknown')}"
                body = json.loads(resp.read().decode() or "{}")
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            return False, f"provider healthcheck failed: {exc}"
        if not isinstance(body, dict):
            return False, "provider healthcheck failed: malformed JSON object"
        ready = body.get("ready")
        if ready is not True:
            return False, str(body.get("error") or "provider not ready")
        supported_assets = body.get("supported_assets")
        supported_chains = body.get("supported_chain_families")
        required_assets = {"BTC", "LTC", "ETH", "USDT"}
        required_chains = {"BTC", "LTC", "ETHEREUM"}
        if isinstance(supported_assets, list):
            advertised = {str(v).upper() for v in supported_assets}
            if not required_assets.issubset(advertised):
                return False, "provider healthcheck missing required supported_assets"
        elif isinstance(supported_chains, list):
            advertised = {str(v).upper() for v in supported_chains}
            if not required_chains.issubset(advertised):
                return False, "provider healthcheck missing required supported_chain_families"
        else:
            return False, "provider healthcheck must declare supported_assets or supported_chain_families"
        return True, None

    def get_or_create_address(self, user_id: int, asset: str) -> IssuedAddress:
        symbol = asset.upper().strip()
        if symbol not in SUPPORTED_ASSETS:
            raise ValueError(f"unsupported asset: {symbol}")
        self._validate_production_config()
        if not self.base_url:
            raise RuntimeError("ADDRESS_PROVIDER_URL is missing")
        payload = json.dumps({"user_id": int(user_id), "asset": symbol}).encode()
        req = Request(f"{self.base_url}/addresses/get-or-create", method="POST", headers=self._headers(), data=payload)
        try:
            with urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode() or "{}")
                if int(getattr(resp, "status", 200)) >= 400:
                    raise RuntimeError(f"provider status={getattr(resp, 'status', 'unknown')}")
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError("address provider request failed") from exc

        if not isinstance(body, dict):
            raise RuntimeError("address provider response malformed")
        address = str(body.get("address") or "").strip()
        provider_ref = str(body.get("provider_ref") or body.get("route_id") or "").strip()
        provider_asset = str(body.get("asset") or symbol).upper().strip()
        provider_chain = str(body.get("chain_family") or ("ETHEREUM" if symbol in {"ETH", "USDT"} else symbol)).upper().strip()
        expected_chain = "ETHEREUM" if symbol in {"ETH", "USDT"} else symbol
        if provider_asset != symbol:
            raise RuntimeError("address provider response asset mismatch")
        if provider_chain != expected_chain:
            raise RuntimeError("address provider response chain mismatch")
        if not address or not provider_ref:
            raise RuntimeError("address provider response missing address/provider_ref")
        return IssuedAddress(address=address, provider_origin="external_http", provider_ref=provider_ref, asset=provider_asset, chain_family=provider_chain)


def build_address_provider() -> AddressProvider:
    provider = os.getenv("ADDRESS_PROVIDER", "auto").strip().lower()
    if provider == "http":
        return HttpAddressProvider()
    if provider in {"local_hd", "local", "seed"}:
        return LocalHDAddressProvider()
    if provider in {"auto", "", "default"}:
        if os.getenv("ADDRESS_PROVIDER_URL", "").strip():
            return HttpAddressProvider()
        if os.getenv("HD_WALLET_SEED_HEX", "").strip():
            return LocalHDAddressProvider()
        return DisabledAddressProvider()
    return DisabledAddressProvider()
