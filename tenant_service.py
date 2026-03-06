from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fee_service import FeeService


@dataclass
class TenantBot:
    bot_id: int
    owner_user_id: int
    bot_display_name: str
    support_contact: str
    bot_extra_fee_percent: Decimal


class TenantService:
    def __init__(self) -> None:
        self._tenants: dict[int, TenantBot] = {}
        self._fee_service = FeeService()

    def create_or_update_tenant(
        self,
        bot_id: int,
        owner_user_id: int,
        bot_display_name: str,
        support_contact: str,
        bot_extra_fee_percent: Decimal,
    ) -> TenantBot:
        validated = self._fee_service.validate_bot_extra_fee_percent(bot_extra_fee_percent)
        tenant = TenantBot(
            bot_id=bot_id,
            owner_user_id=owner_user_id,
            bot_display_name=bot_display_name,
            support_contact=support_contact,
            bot_extra_fee_percent=validated,
        )
        self._tenants[bot_id] = tenant
        return tenant

    def get_tenant(self, bot_id: int) -> TenantBot | None:
        return self._tenants.get(bot_id)
