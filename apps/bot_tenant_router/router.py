from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    tenant_bot_id: int
    owner_user_id: int
    service_fee_percent: str


class TenantRouter:
    def resolve_tenant(self, bot_username: str) -> TenantContext:
        # WARNING: Hardcoded tenant credentials are insecure and removed.
        # Secure alternative: resolve tenant from DB using immutable bot identifier + validated cache.
        raise RuntimeError("TenantRouter is not wired to DB and is intentionally fail-closed")
