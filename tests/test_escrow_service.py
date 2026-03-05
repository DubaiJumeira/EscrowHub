from decimal import Decimal

import pytest

from escrow_service import EscrowService
from fee_service import FeeService
from price_service import StaticPriceService, validate_minimum_escrow_usd
from wallet_service import WalletService
from watchers.eth_watcher import EthWatcher


def test_min_40_usd_validation():
    prices = StaticPriceService({"USDT": Decimal("1")})
    with pytest.raises(ValueError):
        validate_minimum_escrow_usd(prices, "USDT", Decimal("39.99"))
    validate_minimum_escrow_usd(prices, "USDT", Decimal("40"))


def test_fee_calculation_platform_fixed_and_bot_cap():
    fees = FeeService()
    breakdown = fees.calculate_total_fees(Decimal("1000"), Decimal("2"))

    assert breakdown.platform_fee == Decimal("30.00")
    assert breakdown.bot_fee == Decimal("20.00")
    assert breakdown.total_fee == Decimal("50.00")
    assert breakdown.seller_payout == Decimal("950.00")

    with pytest.raises(ValueError):
        fees.calculate_bot_extra_fee(Decimal("100"), Decimal("3.01"))


def test_escrow_release_seller_payout_correctness():
    service = EscrowService()
    service.tenant_service.create_or_update_tenant(
        bot_id=1,
        owner_user_id=900,
        bot_display_name="Tenant",
        support_contact="@support",
        bot_extra_fee_percent=Decimal("2"),
    )
    service.wallet_service.credit_deposit_if_confirmed(
        user_id=10,
        asset="USDT",
        amount=Decimal("1000"),
        txid="tx1",
        chain_family="ETHEREUM",
        unique_key="tx1:0",
        confirmations=12,
        finalized=True,
    )

    escrow = service.create_escrow(bot_id=1, buyer_id=10, seller_id=20, asset="USDT", amount=Decimal("1000"), description="deal")
    service.release(escrow.escrow_id)

    assert service.wallet_service.total_balance(20, "USDT") == Decimal("950.00")
    assert service.wallet_service.account_revenue_balance("PLATFORM_REVENUE", None, "USDT") == Decimal("30.00")
    assert service.wallet_service.account_revenue_balance("BOT_OWNER_REVENUE", 900, "USDT") == Decimal("20.00")


def test_ledger_balance_lock_and_release_correctness():
    w = WalletService()
    w.credit_deposit_if_confirmed(1, "USDT", Decimal("100"), "t1", "ETHEREUM", "t1:0", 12, True)
    w.lock_for_escrow(55, 1, "USDT", Decimal("60"))

    assert w.total_balance(1, "USDT") == Decimal("100")
    assert w.locked_balance(1, "USDT") == Decimal("60")
    assert w.available_balance(1, "USDT") == Decimal("40")

    w.cancel_escrow_lock(55)
    assert w.available_balance(1, "USDT") == Decimal("100")


def test_idempotent_deposit_credit():
    w = WalletService()
    eth = EthWatcher(w)

    first = eth.process_erc20_transfer(user_id=5, asset="USDT", amount=Decimal("50"), txhash="0xabc", log_index=2, confirmations=12)
    second = eth.process_erc20_transfer(user_id=5, asset="USDT", amount=Decimal("50"), txhash="0xabc", log_index=2, confirmations=12)

    assert first is True
    assert second is False
    assert w.total_balance(5, "USDT") == Decimal("50")
