from decimal import Decimal

import pytest
import sqlite3

from escrow_service import EscrowService
from fee_service import FeeService
from hd_wallet import HDWalletDeriver
from infra.db.database import init_db
from price_service import StaticPriceService, validate_minimum_escrow_usd
from signer.signer_service import HDWalletSignerProvider
from tenant_service import TenantService
from wallet_service import WalletService
from watchers.eth_watcher import run_once as run_eth_once


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

    assert escrow.wallet_service.total_balance(seller, "USDT") == Decimal("950.0")
    assert escrow.wallet_service.account_revenue_balance("PLATFORM_REVENUE", None, "USDT") == Decimal("30.0")
    assert escrow.wallet_service.account_revenue_balance("BOT_OWNER_REVENUE", owner, "USDT") == Decimal("20.0")


def test_eth_watcher_entrypoint_no_rpc(conn):
    assert run_eth_once({}) == 0


def test_deterministic_derivation_and_paths(monkeypatch):
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ab" * 32)
    monkeypatch.setenv("APP_ENV", "dev")
    d = HDWalletDeriver()

    btc = d.derive_btc(7)
    btc2 = d.derive_btc(7)
    eth = d.derive_eth(7)
    eth2 = d.derive_eth(7)
    btc_other = d.derive_btc(8)
    ltc = d.derive_ltc(7)

    assert btc.public_address == btc2.public_address
    assert eth.public_address == eth2.public_address
    assert btc.public_address != btc_other.public_address
    assert btc.path != ltc.path
    assert "84'/2'" in ltc.path


def test_usdt_usdc_reuse_eth_address_and_no_private_keys_in_db(conn, monkeypatch):
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ef" * 32)
    monkeypatch.setenv("APP_ENV", "dev")
    wallet = WalletService(conn)

    eth = wallet.get_or_create_deposit_address(10, "ETH")
    usdt = wallet.get_or_create_deposit_address(10, "USDT")
    usdc = wallet.get_or_create_deposit_address(10, "USDC")

    assert eth.address == usdt.address == usdc.address

    row = conn.execute("SELECT * FROM wallet_addresses WHERE user_id=? AND asset='ETH'", (10,)).fetchone()
    assert "private" not in " ".join(row.keys()).lower()


def test_production_fails_when_hdwallet_missing(monkeypatch):
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "aa" * 32)
    monkeypatch.setenv("APP_ENV", "production")
    d = HDWalletDeriver()

    monkeypatch.setattr(d, "_require_hdwallet", lambda: (_ for _ in ()).throw(RuntimeError("hdwallet library missing")))
    with pytest.raises(RuntimeError):
        d.derive_btc(1)


def test_signer_signs_valid_transaction_shape(monkeypatch):
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "cd" * 32)
    monkeypatch.setenv("APP_ENV", "dev")
    signer = HDWalletSignerProvider()
    signed = signer.sign_transaction("ETH", user_id=11, destination_address="0xabc", amount="1.23")
    assert signed.raw_tx_hex.startswith("0x")
    assert len(signed.txid) == 64
