from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

TWOPLACES = Decimal("0.01")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class FeeBreakdown:
    escrow_fee: Decimal
    bot_service_fee: Decimal
    total_fees: Decimal
    platform_revenue: Decimal
    owner_revenue: Decimal
    seller_payout: Decimal


class FeeService:
    """Fee policy service for escrow and multi-tenant revenue split."""

    def __init__(
        self,
        escrow_fee_percent: Decimal = Decimal("3"),
        platform_share_percent: Decimal = Decimal("30"),
        owner_share_percent: Decimal = Decimal("70"),
    ) -> None:
        if platform_share_percent + owner_share_percent != HUNDRED:
            raise ValueError("platform and owner shares must sum to 100")
        self.escrow_fee_percent = Decimal(escrow_fee_percent)
        self.platform_share_percent = Decimal(platform_share_percent)
        self.owner_share_percent = Decimal(owner_share_percent)

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)

    def calculate_fee(self, amount: Decimal, bot_service_fee_percent: Decimal) -> FeeBreakdown:
        amount = Decimal(amount)
        bot_service_fee_percent = Decimal(bot_service_fee_percent)

        escrow_fee = self._money(amount * self.escrow_fee_percent / HUNDRED)
        bot_service_fee = self._money(amount * bot_service_fee_percent / HUNDRED)
        platform_revenue, owner_revenue = self.split_revenue(bot_service_fee)
        total_fees = self._money(escrow_fee + bot_service_fee)
        seller_payout = self._money(amount - total_fees)

        return FeeBreakdown(
            escrow_fee=escrow_fee,
            bot_service_fee=bot_service_fee,
            total_fees=total_fees,
            platform_revenue=platform_revenue,
            owner_revenue=owner_revenue,
            seller_payout=seller_payout,
        )

    def split_revenue(self, service_fee_revenue: Decimal) -> tuple[Decimal, Decimal]:
        service_fee_revenue = Decimal(service_fee_revenue)
        platform = self._money(service_fee_revenue * self.platform_share_percent / HUNDRED)
        owner = self._money(service_fee_revenue - platform)
        return platform, owner
