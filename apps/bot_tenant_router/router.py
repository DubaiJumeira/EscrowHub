from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TenantContext:
    tenant_bot_id: int
    owner_user_id: int
    service_fee_percent: str


class TenantRouter:
    """Routes Telegram update metadata to tenant config from DB/cache (stub)."""

    def resolve_tenant(self, bot_username: str) -> TenantContext:
        # TODO: Replace with DB query + cache.
        return TenantContext(tenant_bot_id=1, owner_user_id=1001, service_fee_percent="2.5")
