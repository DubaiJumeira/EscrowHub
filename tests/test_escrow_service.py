from decimal import Decimal

import pytest

from escrow_service import EscrowService
from fee_service import FeeService
from price_service import StaticPriceService, validate_minimum_escrow_usd


def test_min_40_usd_validation():
    prices = StaticPriceService({"USDT": Decimal("1")})
    with pytest.raises(ValueError):
        validate_minimum_escrow_usd(prices, "USDT", Decimal("39.99"))
    validate_minimum_escrow_usd(prices, "USDT", Decimal("40"))


def test_fee_calculation_platform_and_bot_extra():
    fees = FeeService()
    breakdown = fees.calculate_total_fees(Decimal("1000"), Decimal("2"))

    assert breakdown.platform_fee == Decimal("30.00")
    assert breakdown.bot_extra_fee == Decimal("20.00")
    assert breakdown.total_fee == Decimal("50.00")
    assert breakdown.seller_payout == Decimal("950.00")


def test_bot_extra_fee_cap_enforced():
    fees = FeeService()
    with pytest.raises(ValueError):
        fees.calculate_bot_extra_fee(Decimal("100"), Decimal("3.01"))


def test_release_seller_payout_correctness():
    service = EscrowService()
    service.tenant_service.create_or_update_tenant(
        bot_id=1,
        owner_user_id=900,
        bot_display_name="Tenant",
        support_contact="@support",
        bot_extra_fee_percent=Decimal("2"),
    )

    service.wallet_service.credit_deposit(user_id=10, asset="USDT", amount=Decimal("1000"), tx_hash="tx1", confirmations=12)
    escrow = service.create_escrow(bot_id=1, buyer_id=10, seller_id=20, asset="USDT", amount=Decimal("1000"), description="deal")
    released = service.release(escrow.escrow_id)

    seller_balance = service.wallet_service.get_balances_for_user(20)["USDT"].available
    owner_balance = service.wallet_service.get_balances_for_user(900)["USDT"].available

    assert released.fee_breakdown.platform_fee == Decimal("30.00")
    assert released.fee_breakdown.bot_extra_fee == Decimal("20.00")
    assert seller_balance == Decimal("950.00")
    assert owner_balance == Decimal("20.00")
