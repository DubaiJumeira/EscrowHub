from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlite3

from bot import _RATE_BUCKETS, _clear_draft_flow, _is_rate_limited, _is_user_frozen, _notify_safe, _render_user_profile, _user_profile
from escrow_service import EscrowService
from fee_service import FeeService
from hd_wallet import HDWalletDeriver
from infra.chain_adapters.eth_rpc import TRANSFER_TOPIC, EthRpcAdapter
from infra.db.database import init_db
from price_service import StaticPriceService, validate_minimum_escrow_usd
from signer.signer_service import HDWalletSignerProvider
from tenant_service import TenantService
from wallet_service import WalletService
from watchers.eth_watcher import run_once as run_eth_once
from watchers.notify import notify_deposit_credited
from watcher_status_service import read_watcher_status, upsert_watcher_status


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

    row = conn.execute("SELECT * FROM wallet_addresses WHERE address=? AND asset='ETH'", (eth.address,)).fetchone()
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


def test_watcher_status_persistence(conn):
    upsert_watcher_status(conn, "btc_watcher", success=False, error="rpc down")
    upsert_watcher_status(conn, "btc_watcher", success=True)
    status = read_watcher_status(conn, ["btc_watcher", "eth_watcher"])
    assert status["btc_watcher"]["consecutive_failures"] == 0
    assert status["eth_watcher"]["last_run_at"] is None


def _seed_tenant(conn):
    tenant = TenantService(conn)
    buyer = tenant.ensure_user(100, "buyer")
    seller = tenant.ensure_user(200, "seller")
    owner = tenant.ensure_user(300, "owner")
    tenant.create_or_update_tenant(1, owner, "bot", "@support", Decimal("2"))
    return tenant, buyer, seller, owner


def test_create_escrow_defaults_to_pending(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "tx1", "tx1:0", "ETHEREUM", 12, True)

    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    row = conn.execute("SELECT status FROM escrows WHERE id=?", (view.escrow_id,)).fetchone()

    assert view.status == "pending"
    assert row["status"] == "pending"


def test_release_moves_pending_to_completed(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "tx2", "tx2:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("60"), "deal")

    released = escrow.release(view.escrow_id, actor_user_id=buyer)

    assert released.status == "completed"
    assert escrow.get_escrow(view.escrow_id)["status"] == "completed"


def test_buyer_cannot_release_another_users_escrow(conn):
    tenant, buyer, seller, _ = _seed_tenant(conn)
    attacker = tenant.ensure_user(400, "attacker")
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "tx3", "tx3:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")

    with pytest.raises(ValueError):
        escrow.release(view.escrow_id, actor_user_id=attacker)


def test_review_uniqueness_per_reviewer_per_escrow(conn):
    conn.execute("INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)", (1, 2, 9, 5))
    conn.execute("INSERT OR IGNORE INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)", (1, 2, 9, 4))
    count = conn.execute("SELECT COUNT(*) c FROM reviews WHERE reviewer_id=1 AND escrow_id=9").fetchone()["c"]
    assert count == 1


def test_average_rating_calculation_from_reviews(conn):
    tenant, _, seller, _ = _seed_tenant(conn)
    conn.execute("INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)", (10, seller, 101, 5))
    conn.execute("INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)", (11, seller, 102, 3))
    user_row = conn.execute("SELECT * FROM users WHERE id=?", (seller,)).fetchone()

    profile = _user_profile(conn, user_row)

    assert profile["rating"] == 4.0


def test_notifications_failure_does_not_raise_and_db_can_commit(conn):
    class FailingBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("telegram offline")

    context = SimpleNamespace(bot=FailingBot())

    import asyncio

    asyncio.run(_notify_safe(context, 123, "hello"))
    conn.execute("INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)", (8, 9, 10, 5))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 1


def test_create_escrow_validates_available_balance(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("50"), "tx4", "tx4:0", "ETHEREUM", 12, True)

    with pytest.raises(ValueError):
        escrow.create_escrow(1, buyer, seller, "USDT", Decimal("60"), "too high")


def test_check_user_profile_render_uses_db_metrics(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("200"), "txp", "txp:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("80"), "goods")
    escrow.release(view.escrow_id, actor_user_id=buyer)
    conn.execute("INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)", (buyer, seller, view.escrow_id, 5))

    row = conn.execute("SELECT * FROM users WHERE id=?", (seller,)).fetchone()
    profile = _user_profile(conn, row)
    rendered = _render_user_profile(profile)

    assert "@seller" in rendered
    assert "Rating: Too few reviews" in rendered
    assert "Completed deals: 1" in rendered


def test_cancel_flow_clears_only_draft_keys():
    context = SimpleNamespace(user_data={
        "seller_id": 1,
        "amount": Decimal("10"),
        "custom": "keep",
    })
    removed = _clear_draft_flow(context)
    assert removed is True
    assert "seller_id" not in context.user_data
    assert "amount" not in context.user_data
    assert context.user_data["custom"] == "keep"


def test_is_user_frozen_reads_db_flag(conn):
    conn.execute("INSERT INTO users(telegram_id, username, frozen) VALUES(?,?,?)", (999, "frozen_user", 1))
    assert _is_user_frozen(conn, 999) is True
    assert _is_user_frozen(conn, 1000) is False


def test_deposit_notify_failure_is_swallowed(monkeypatch, conn):
    conn.execute("INSERT INTO users(id, telegram_id, username, frozen) VALUES(?,?,?,0)", (42, 42, "u42"))

    class Boom:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("net down")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr("watchers.notify.request.urlopen", Boom())

    notify_deposit_credited(conn, 42, "USDT", Decimal("5"), Decimal("10"))


def test_wallet_resolves_telegram_id_to_internal_user_id(conn):
    tenant = TenantService(conn)
    internal_id = tenant.ensure_user(777, "alice")
    wallet = WalletService(conn)
    wallet.credit_deposit_if_confirmed(777, "USDT", Decimal("50"), "txz", "txz:0", "ETHEREUM", 12, True)

    assert wallet.available_balance(internal_id, "USDT") == Decimal("50")


def test_erc20_transfer_topic_constant_is_correct():
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def test_eth_rpc_adapter_parses_erc20_transfer_event(monkeypatch):
    monkeypatch.setenv("USDT_ERC20_CONTRACT", "0xdac17f958d2ee523a2206206994597c13d831ec7")
    adapter = EthRpcAdapter({"0x1111111111111111111111111111111111111111": 99})
    event = {
        "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "topics": [
            TRANSFER_TOPIC,
            "0x000000000000000000000000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "0x0000000000000000000000001111111111111111111111111111111111111111",
        ],
        "transactionHash": "0xabc",
        "logIndex": "0x1",
        "value": hex(1_500_000),
        "confirmations": 20,
    }
    dep = adapter._parse_event(event)
    assert dep is not None
    assert dep.asset == "USDT"
    assert dep.user_id == 99
    assert dep.amount == Decimal("1.5")


def test_rate_limiter_bucket_eviction(monkeypatch):
    _RATE_BUCKETS.clear()
    fake_time = [1000.0]

    def _now():
        return fake_time[0]

    monkeypatch.setattr("bot.time.time", _now)
    assert _is_rate_limited(1, "x", limit=2, window_s=10) is False
    assert _is_rate_limited(1, "x", limit=2, window_s=10) is False
    assert _is_rate_limited(1, "x", limit=2, window_s=10) is True
    fake_time[0] = 1200.0
    assert _is_rate_limited(2, "y", limit=2, window_s=10) is False
    # old key should be pruned after grace window
    assert (1, "x") not in _RATE_BUCKETS
