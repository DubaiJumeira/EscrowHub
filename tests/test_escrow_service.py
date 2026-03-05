from decimal import Decimal

import pytest

from core.fees.service import FeeService
from core.pricing.service import StaticPriceService, validate_minimum_usd


def test_fee_calculation_with_base_and_service_fee():
    fee_service = FeeService()
    breakdown = fee_service.calculate_fee(amount=Decimal("1000"), bot_service_fee_percent=Decimal("10"))

    assert breakdown.escrow_fee == Decimal("30.00")
    assert breakdown.bot_service_fee == Decimal("100.00")
    assert breakdown.total_fees == Decimal("130.00")
    assert breakdown.seller_payout == Decimal("870.00")


def test_fee_split_platform_owner_ratio():
    fee_service = FeeService()
    platform, owner = fee_service.split_revenue(Decimal("100.00"))

    assert platform == Decimal("30.00")
    assert owner == Decimal("70.00")


def test_minimum_40_usd_validation_rejects_under_threshold():
    price_service = StaticPriceService({"USDT": Decimal("1")})

    with pytest.raises(ValueError):
        validate_minimum_usd(price_service, asset="USDT", amount=Decimal("39.99"), minimum_usd=Decimal("40"))


def test_minimum_40_usd_validation_accepts_threshold():
    price_service = StaticPriceService({"USDT": Decimal("1")})

    validate_minimum_usd(price_service, asset="USDT", amount=Decimal("40.00"), minimum_usd=Decimal("40"))
