from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config.settings import Settings

SUPPORTED_ASSETS = {"BTC", "LTC", "ETH", "USDT"}


@dataclass(frozen=True)
class IssuedAddress:
    address: str
    provider_origin: str
    provider_ref: str


class AddressProvider:
    def is_ready(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    def get_or_create_address(self, user_id: int, asset: str) -> IssuedAddress:
        raise NotImplementedError


class DisabledAddressProvider(AddressProvider):
    def is_ready(self) -> tuple[bool, str | None]:
        return False, "no approved external address provider configured"

    def get_or_create_address(self, user_id: int, asset: str) -> IssuedAddress:
        # WARNING: Production deposit issuance must not derive locally from hot runtime seed paths.
        # Secure alternative: use a hardened external address service with idempotent per-user issuance.
        raise RuntimeError("deposit address provider is disabled")


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
        ready = body.get("ready")
        if ready is not True:
            return False, str(body.get("error") or "provider not ready")
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

        address = str(body.get("address") or "").strip()
        provider_ref = str(body.get("provider_ref") or body.get("route_id") or "").strip()
        if not address or not provider_ref:
            raise RuntimeError("address provider response missing address/provider_ref")
        return IssuedAddress(address=address, provider_origin="external_http", provider_ref=provider_ref)


def build_address_provider() -> AddressProvider:
    provider = os.getenv("ADDRESS_PROVIDER", "disabled").strip().lower()
    if provider == "http":
        return HttpAddressProvider()
    if Settings.is_production:
        return DisabledAddressProvider()
    return DisabledAddressProvider()
