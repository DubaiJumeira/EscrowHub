from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

HUNDRED = Decimal("100")
TWOPLACES = Decimal("0.01")
PLATFORM_FEE_PERCENT = Decimal("3")
MAX_BOT_EXTRA_FEE_PERCENT = Decimal("3")


@dataclass(frozen=True)
class FeeBreakdown:
    platform_fee: Decimal
    bot_fee: Decimal
    total_fee: Decimal
    seller_payout: Decimal


class FeeService:
    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return Decimal(value).quantize(TWOPLACES, rounding=ROUND_HALF_UP)

    def validate_bot_extra_fee_percent(self, bot_extra_fee_percent: Decimal) -> Decimal:
        pct = Decimal(bot_extra_fee_percent)
        if pct < 0 or pct > MAX_BOT_EXTRA_FEE_PERCENT:
            raise ValueError("bot_extra_fee_percent must be between 0 and 3")
        return pct

    def calculate_platform_fee(self, amount: Decimal) -> Decimal:
        return self._money(Decimal(amount) * PLATFORM_FEE_PERCENT / HUNDRED)

    def calculate_bot_extra_fee(self, amount: Decimal, bot_extra_fee_percent: Decimal) -> Decimal:
        pct = self.validate_bot_extra_fee_percent(bot_extra_fee_percent)
        return self._money(Decimal(amount) * pct / HUNDRED)

    def calculate_total_fees(self, amount: Decimal, bot_extra_fee_percent: Decimal) -> FeeBreakdown:
        amount = Decimal(amount)
        platform_fee = self.calculate_platform_fee(amount)
        bot_fee = self.calculate_bot_extra_fee(amount, bot_extra_fee_percent)
        total_fee = self._money(platform_fee + bot_fee)
        seller_payout = self._money(amount - total_fee)
        return FeeBreakdown(platform_fee=platform_fee, bot_fee=bot_fee, total_fee=total_fee, seller_payout=seller_payout)

