from decimal import Decimal

import pytest
import sqlite3

from escrow_service import EscrowService
from fee_service import FeeService
from infra.db.database import init_db
from price_service import StaticPriceService, validate_minimum_escrow_usd
from tenant_service import TenantService
from wallet_service import WalletService


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    yield c
    c.close()


def test_fee_calculations():
    s = FeeService()
    b = s.calculate_total_fees(Decimal("1000"), Decimal("2"))
    assert b.platform_fee == Decimal("30.00")
    assert b.bot_fee == Decimal("20.00")
    assert b.seller_payout == Decimal("950.00")


def test_minimum_40_validation():
    ps = StaticPriceService({"USDT": Decimal("1")})
    with pytest.raises(ValueError):
        validate_minimum_escrow_usd(ps, "USDT", Decimal("39.99"))


def test_ledger_lock_release_and_idempotent_deposit(conn):
    wallet = WalletService(conn)
    wallet.credit_deposit_if_confirmed(1, "USDT", Decimal("100"), "tx", "tx:0", "ETHEREUM", 12, True)
    assert wallet.credit_deposit_if_confirmed(1, "USDT", Decimal("100"), "tx", "tx:0", "ETHEREUM", 12, True) is False
    wallet.lock_for_escrow(5, 1, "USDT", Decimal("60"))
    assert wallet.available_balance(1, "USDT") == Decimal("40.0")
    assert wallet.locked_balance(1, "USDT") == Decimal("60.0")


def test_withdrawal_reserve_prevents_overspend(conn):
    wallet = WalletService(conn)
    wallet.credit_deposit_if_confirmed(1, "USDT", Decimal("100"), "tx2", "tx2:0", "ETHEREUM", 12, True)
    wallet.request_withdrawal(1, "USDT", Decimal("60"), "0xabc")
    with pytest.raises(ValueError):
        wallet.request_withdrawal(1, "USDT", Decimal("50"), "0xdef")


def test_dispute_resolution_outcomes(conn):
    tenant = TenantService(conn)
    buyer = tenant.ensure_user(100)
    seller = tenant.ensure_user(200)
    owner = tenant.ensure_user(300)
    admin = tenant.ensure_user(400)
    tenant.create_or_update_tenant(1, owner, "bot", "@support", Decimal("2"))

    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("1000"), "tx1", "tx1:0", "ETHEREUM", 12, True)
    e = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("1000"), "deal")
    escrow.dispute(e.escrow_id, buyer, "issue")
    escrow.resolve_dispute(e.escrow_id, admin, "release_seller")

    assert escrow.wallet_service.total_balance(seller, "USDT") == Decimal("1000.0")
    assert escrow.wallet_service.account_revenue_balance("PLATFORM_REVENUE", None, "USDT") == Decimal("0")
    assert escrow.wallet_service.account_revenue_balance("BOT_OWNER_REVENUE", owner, "USDT") == Decimal("0")
