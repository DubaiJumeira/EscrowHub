from decimal import Decimal

import pytest
import sqlite3

from escrow_service import EscrowService
from fee_service import FeeService
from infra.chain_adapters.base import ChainDeposit
from infra.db.database import init_db
from price_service import StaticPriceService, validate_minimum_escrow_usd
from tenant_service import TenantService
from wallet_service import WalletService
from watchers import eth_watcher


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "11" * 32)
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


def test_deterministic_btc_eth_derivation_and_no_private_persistence(conn):
    wallet = WalletService(conn)
    a1 = wallet.get_or_create_deposit_address(1, "BTC")
    a1b = wallet.get_or_create_deposit_address(1, "BTC")
    a2 = wallet.get_or_create_deposit_address(2, "BTC")

    e1 = wallet.get_or_create_deposit_address(1, "ETH")
    e1b = wallet.get_or_create_deposit_address(1, "ETH")
    e2 = wallet.get_or_create_deposit_address(2, "ETH")

    assert a1.address == a1b.address
    assert a1.address != a2.address
    assert e1.address == e1b.address
    assert e1.address != e2.address

    cols = [r[1] for r in conn.execute("PRAGMA table_info(wallet_addresses)").fetchall()]
    forbidden = {"private_key", "xprv", "seed", "mnemonic"}
    assert forbidden.intersection(set(cols)) == set()


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


def test_eth_watcher_cursor_resume(monkeypatch, conn):
    class DummyAdapter:
        def __init__(self, _):
            self.rpc_url = "http://dummy"

        def get_latest_block(self):
            return 20

        def fetch_deposits_between(self, start, end):
            if start == 11 and end == 20:
                return [ChainDeposit(1, "ETH", Decimal("1"), "0x1", "0x1:to:1", 12, True)]
            return []

    class ConnProxy:
        def __init__(self, base):
            self._b = base

        def __getattr__(self, item):
            return getattr(self._b, item)

        def close(self):
            return

    proxy = ConnProxy(conn)
    monkeypatch.setattr(eth_watcher, "EthRpcAdapter", DummyAdapter)
    monkeypatch.setattr(eth_watcher, "get_connection", lambda: proxy)
    monkeypatch.setattr(eth_watcher, "init_db", lambda _: None)
    conn.execute("INSERT INTO chain_scan_state(chain_family,cursor) VALUES('ETHEREUM','10')")
    conn.commit()

    credits = eth_watcher.run_once({"0xabc": 1}, batch_size=10)
    assert credits == 1

    row = conn.execute("SELECT cursor FROM chain_scan_state WHERE chain_family='ETHEREUM'").fetchone()
    assert row["cursor"] == "20"
