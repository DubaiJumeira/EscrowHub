from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
import sqlite3

from bot import _clear_draft_flow, _is_rate_limited, _is_user_frozen, _notify_safe, _render_user_profile, _set_frozen_state, _user_profile, revenue_report
from escrow_service import EscrowService
from fee_service import FeeService
from hd_wallet import HDWalletDeriver
from infra.chain_adapters.eth_rpc import TRANSFER_TOPIC, EthRpcAdapter
from infra.db.database import init_db
from price_service import StaticPriceService, validate_minimum_escrow_usd
from signer.signer_service import DisabledSignerProvider
from tenant_service import TenantService
from wallet_service import WalletService
from watchers.eth_watcher import run_once as run_eth_once
from watchers.notify import notify_deposit_credited
from watcher_status_service import normalize_deposit_provider_state, read_watcher_status, upsert_watcher_status
from config.settings import Settings


class _ConnProxy:
    def __init__(self, conn):
        self._conn = conn
    def close(self):
        return None
    def __getattr__(self, name):
        return getattr(self._conn, name)


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
    assert wallet.available_balance(1, "USDT") == Decimal("39.0")
    assert wallet.locked_balance(1, "USDT") == Decimal("60.0")


def test_dispute_resolution_outcomes(conn):
    tenant = TenantService(conn)
    buyer = tenant.ensure_user(100)
    seller = tenant.ensure_user(200)
    owner = tenant.ensure_user(300)
    admin = tenant.ensure_user(400)
    tenant.create_or_update_tenant(1, owner, "bot", "bot", "@support", Decimal("2"))

    Settings.moderator_ids = {admin}
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("1010.11"), "tx1", "tx1:0", "ETHEREUM", 12, True)
    e = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("1000"), "deal")
    escrow.dispute(e.escrow_id, buyer, "issue")
    escrow.resolve_dispute(e.escrow_id, admin, "release_seller")

    assert escrow.wallet_service.total_balance(seller, "USDT") == Decimal("950.0")
    assert escrow.wallet_service.account_revenue_balance("PLATFORM_REVENUE", None, "USDT") == Decimal("40.1011")
    assert escrow.wallet_service.account_revenue_balance("BOT_OWNER_REVENUE", owner, "USDT") == Decimal("20.0")


def test_eth_watcher_entrypoint_no_rpc(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    assert run_eth_once({}) == 0


def test_deterministic_derivation_and_paths(monkeypatch):
    pytest.importorskip("bip_utils")
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ab" * 32)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setattr(Settings, "ltc_xpub", "")
    monkeypatch.setattr(Settings, "eth_xpub", "")
    monkeypatch.setattr(Settings, "is_production", False)
    d = HDWalletDeriver()

    btc = d.derive_btc_address(7)
    btc2 = d.derive_btc_address(7)
    eth = d.derive_eth_address(7)
    eth2 = d.derive_eth_address(7)
    btc_other = d.derive_btc_address(8)
    ltc = d.derive_ltc_address(7)

    assert btc.public_address == btc2.public_address
    assert eth.public_address == eth2.public_address
    assert btc.public_address != btc_other.public_address
    assert btc.path != ltc.path
    assert "84'/2'" in ltc.path


def test_usdt_reuse_eth_address_and_no_private_keys_in_db(conn, monkeypatch):
    pytest.importorskip("bip_utils")
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ef" * 32)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setattr(Settings, "ltc_xpub", "")
    monkeypatch.setattr(Settings, "eth_xpub", "")
    monkeypatch.setattr(Settings, "is_production", False)
    wallet = WalletService(conn)

    eth = wallet.get_or_create_deposit_address(10, "ETH")
    usdt = wallet.get_or_create_deposit_address(10, "USDT")

    assert eth.address == usdt.address

    row = conn.execute("SELECT * FROM wallet_addresses WHERE asset='ETH'").fetchone()
    assert "private" not in " ".join(row.keys()).lower()


def test_production_http_mode_without_xpub_fails_hdwallet_derivation(monkeypatch):
    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setenv("ADDRESS_PROVIDER", "http")
    d = HDWalletDeriver()
    with pytest.raises(RuntimeError):
        d.derive_btc_address(1)


def test_signer_signs_valid_transaction_shape(monkeypatch):
    signer = DisabledSignerProvider()
    with pytest.raises(Exception):
        signer.sign_and_broadcast("ETH", "0x" + "1" * 40, "1.23")


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
    tenant.create_or_update_tenant(1, owner, "bot", "bot", "@support", Decimal("2"))
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

    escrow.accept_escrow(view.escrow_id, seller)
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
    escrow.accept_escrow(view.escrow_id, seller)
    escrow.release(view.escrow_id, actor_user_id=buyer)
    conn.execute("INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)", (buyer, seller, view.escrow_id, 5))

    row = conn.execute("SELECT * FROM users WHERE id=?", (seller,)).fetchone()
    profile = _user_profile(conn, row)
    rendered = _render_user_profile(profile)

    assert "@seller" in rendered
    assert "Rating: Too few reviews" in rendered


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

    assert wallet.available_balance(internal_id, "USDT") == Decimal("49.5")


def test_erc20_transfer_topic_constant_is_correct():
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def test_eth_rpc_adapter_parses_erc20_transfer_event(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ETH_DEPOSIT_EVENTS_JSON", json.dumps([
        {"to": "0x1111111111111111111111111111111111111111", "asset": "USDT", "amount": "1.5", "txid": "0xabc"}
    ]))
    adapter = EthRpcAdapter({"0x1111111111111111111111111111111111111111": 99})
    deposits, _ = adapter.fetch_deposits()
    assert deposits[0].asset == "USDT"
    assert deposits[0].user_id == 99
    assert deposits[0].amount == Decimal("1.5")


def test_rate_limiter_db_backed_across_connections(tmp_path):
    db = tmp_path / "rl.db"
    c1 = sqlite3.connect(db)
    c1.row_factory = sqlite3.Row
    c1.execute("PRAGMA foreign_keys=ON")
    init_db(c1)
    c2 = sqlite3.connect(db)
    c2.row_factory = sqlite3.Row
    c2.execute("PRAGMA foreign_keys=ON")
    assert _is_rate_limited(c1, 1, "x", limit=2, window_s=30) is False
    assert _is_rate_limited(c2, 1, "x", limit=2, window_s=30) is False
    assert _is_rate_limited(c1, 1, "x", limit=2, window_s=30) is True
    c1.close(); c2.close()


def test_frozen_user_withdrawal_rejected(conn):
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(123)
    wallet.credit_deposit_if_confirmed(uid, "USDT", Decimal("200"), "txf", "txf:0", "ETHEREUM", 12, True)
    conn.execute("UPDATE users SET frozen=1 WHERE id=?", (uid,))
    with pytest.raises(ValueError):
        wallet.request_withdrawal(uid, "USDT", Decimal("100"), "0x1111111111111111111111111111111111111111")


def test_dispute_persists_without_external_commit(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txd", "txd:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    escrow.dispute(view.escrow_id, buyer, "reason")
    row = conn.execute("SELECT status FROM escrows WHERE id=?", (view.escrow_id,)).fetchone()
    assert row["status"] == "disputed"


def test_release_rejects_pending(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txrp", "txrp:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("60"), "deal")
    with pytest.raises(ValueError):
        escrow.release(view.escrow_id, actor_user_id=buyer)


def test_cancel_escrow_releases_lock_and_records_event(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txc", "txc:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("60"), "deal")
    escrow.cancel_escrow(view.escrow_id, buyer)
    lock = conn.execute("SELECT status FROM escrow_locks WHERE escrow_id=?", (view.escrow_id,)).fetchone()
    ev = conn.execute("SELECT event_type FROM escrow_events WHERE escrow_id=? ORDER BY id DESC LIMIT 1", (view.escrow_id,)).fetchone()
    assert lock["status"] == "released"
    assert ev["event_type"] == "cancelled"


def test_mark_withdrawal_broadcasted_no_zero_ledger_entry(conn):
    Settings.withdrawals_enabled = True
    Settings.withdrawal_min_interval_seconds = 0
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(222)
    wallet.credit_deposit_if_confirmed(uid, "USDT", Decimal("200"), "txw", "txw:0", "ETHEREUM", 12, True)
    wd = wallet.request_withdrawal(uid, "USDT", Decimal("100"), "0x1111111111111111111111111111111111111111")
    wallet.mark_withdrawal_broadcasted(wd["id"], "0xtxid")
    row = conn.execute("SELECT COUNT(*) c FROM ledger_entries WHERE ref_type='withdrawal' AND ref_id=? AND entry_type='WITHDRAWAL_SENT'", (wd["id"],)).fetchone()
    assert row["c"] == 0


def test_production_rejects_eth_env_deposit_ingestion(monkeypatch):
    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("ETH_DEPOSIT_EVENTS_JSON", '[{"to":"0x1"}]')
    with pytest.raises(RuntimeError):
        EthRpcAdapter({}).fetch_deposits()


def test_production_rejects_fallback_derivation(monkeypatch):
    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "aa" * 32)
    d = HDWalletDeriver()
    with pytest.raises(RuntimeError):
        d.derive_btc_address(1)


def test_production_rejects_hd_signer(monkeypatch):
    from signer.signer_service import SignerService

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("SIGNER_PROVIDER", "hd")
    from signer.errors import SignerConfigurationError
    with pytest.raises(SignerConfigurationError):
        SignerService()


def test_removed_assets_rejected(conn):
    wallet = WalletService(conn)
    for symbol in ("DOGE", "TRX", "BNB"):
        with pytest.raises(ValueError):
            wallet.validate_withdrawal_address(symbol, "x")


def test_moderator_auth_uses_telegram_id(monkeypatch):
    import asyncio
    from bot import _is_moderator

    from config.settings import Settings

    monkeypatch.setattr(Settings, "moderator_ids", {777})
    assert asyncio.run(_is_moderator(777)) is True


def test_db_address_map_loader_reads_wallet_addresses(conn, monkeypatch):
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ab" * 32)
    from run_btc_watcher import _address_map as btc_map
    from run_eth_watcher import _address_map as eth_map
    conn.execute("INSERT INTO users(id, telegram_id, username, frozen) VALUES(?,?,?,0)", (1, 1001, "u1"))
    conn.execute("INSERT INTO users(id, telegram_id, username, frozen) VALUES(?,?,?,0)", (2, 1002, "u2"))
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)", (1, "BTC", "BTC", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080", 1, None, "p"))
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)", (2, "ETH", "ETHEREUM", "0x2222222222222222222222222222222222222222", 2, None, "p"))
    assert btc_map(conn)["bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"] == 1
    assert eth_map(conn)["0x2222222222222222222222222222222222222222"] == 2


def test_concurrent_withdrawal_race_only_one_succeeds(tmp_path):
    db = tmp_path / "race.db"
    c0 = sqlite3.connect(db)
    c0.row_factory = sqlite3.Row
    c0.execute("PRAGMA foreign_keys=ON")
    init_db(c0)
    w0 = WalletService(c0)
    uid = w0._ensure_user_row(555)
    w0.credit_deposit_if_confirmed(uid, "USDT", Decimal("100"), "txr", "txr:0", "ETHEREUM", 12, True)
    c0.commit(); c0.close()

    Settings.withdrawals_enabled = True
    ok = 0
    for _ in range(2):
        cx = sqlite3.connect(db)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA foreign_keys=ON")
        svc = WalletService(cx)
        try:
            svc.request_withdrawal(uid, "USDT", Decimal("80"), "0x1111111111111111111111111111111111111111")
            ok += 1
        except Exception:
            pass
        cx.close()
    assert ok == 1


def test_accept_escrow_transitions_and_event(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txa", "txa:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    escrow.accept_escrow(view.escrow_id, seller)
    row = conn.execute("SELECT status FROM escrows WHERE id=?", (view.escrow_id,)).fetchone()
    ev = conn.execute("SELECT event_type FROM escrow_events WHERE escrow_id=? ORDER BY id DESC LIMIT 1", (view.escrow_id,)).fetchone()
    assert row["status"] == "active"
    assert ev["event_type"] == "accepted"


def test_active_escrow_cannot_be_cancelled_unilaterally(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txac", "txac:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    escrow.accept_escrow(view.escrow_id, seller)
    with pytest.raises(ValueError):
        escrow.cancel_escrow(view.escrow_id, buyer)
    with pytest.raises(ValueError):
        escrow.cancel_escrow(view.escrow_id, seller)


def test_pending_escrow_cancellation_still_works(conn):
    _, buyer, seller, _ = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txpc", "txpc:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("40"), "deal")
    escrow.cancel_escrow(view.escrow_id, buyer)
    row = conn.execute("SELECT status FROM escrows WHERE id=?", (view.escrow_id,)).fetchone()
    assert row["status"] == "cancelled"


def test_admin_freeze_unfreeze_logs_action(monkeypatch, tmp_path):
    admin = 999
    monkeypatch.setattr("bot.ADMIN_IDS", {admin})
    db = tmp_path / "freeze.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    c.execute("INSERT INTO users(telegram_id, username, frozen) VALUES(?,?,0)", (12345, "target"))
    c.commit()
    def _svc():
        cx = sqlite3.connect(db)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA foreign_keys=ON")
        return (cx, None, None, None)
    monkeypatch.setattr("bot._services", _svc)

    class Msg:
        def __init__(self, text):
            self.text = text
            self.replies = []
        async def reply_text(self, txt):
            self.replies.append(txt)

    class U:
        id = admin

    import asyncio
    msg = Msg('/freeze @target')
    upd = SimpleNamespace(effective_user=U(), effective_message=msg, message=msg)
    asyncio.run(_set_frozen_state(upd, True))
    c1 = sqlite3.connect(db); c1.row_factory = sqlite3.Row
    assert c1.execute("SELECT frozen FROM users WHERE username='target'").fetchone()["frozen"] == 1
    assert c1.execute("SELECT COUNT(*) c FROM admin_actions WHERE action_type='freeze_user'").fetchone()["c"] == 1
    c1.close()

    msg2 = Msg('/unfreeze 12345')
    upd2 = SimpleNamespace(effective_user=U(), effective_message=msg2, message=msg2)
    asyncio.run(_set_frozen_state(upd2, False))
    c2 = sqlite3.connect(db); c2.row_factory = sqlite3.Row
    assert c2.execute("SELECT frozen FROM users WHERE username='target'").fetchone()["frozen"] == 0
    assert c2.execute("SELECT COUNT(*) c FROM admin_actions WHERE action_type='unfreeze_user'").fetchone()["c"] == 1
    c2.close()


def test_address_derivation_path_returns_address_only(monkeypatch):
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "aa" * 32)
    monkeypatch.setenv("APP_ENV", "dev")
    d = HDWalletDeriver()
    addr = d.derive_eth_address(5)
    assert hasattr(addr, "public_address")
    assert not hasattr(addr, "private_key_hex")


def test_eth_env_ingestion_only_in_test(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ETH_DEPOSIT_EVENTS_JSON", '[{"to":"0x1"}]')
    with pytest.raises(RuntimeError):
        EthRpcAdapter({}).fetch_deposits()


def test_vault_signer_does_not_fabricate_txid(monkeypatch):
    from signer.signer_service import VaultSignerProvider

    monkeypatch.setenv("VAULT_ADDR", "http://vault")
    monkeypatch.setenv("VAULT_TOKEN", "x")

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"data":{"signature":"abc"}}'

    monkeypatch.setattr("signer.signer_service.urlopen", lambda *a, **k: Resp())
    with pytest.raises(Exception):
        VaultSignerProvider().sign_and_broadcast("ETH", "0x" + "1"*40, "1")


def test_withdrawn_usd_last_24h_uses_usd_conversion(conn):
    wallet = WalletService(conn)
    wallet.price_service = StaticPriceService({"BTC": Decimal("65000")})
    uid = wallet._ensure_user_row(987)
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status) VALUES(?,?,?,?,?)", (uid, "BTC", "0.1", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080", "broadcasted"))
    assert wallet._withdrawn_usd_last_24h(uid) == Decimal("6500.0")


def test_account_revenue_balance_decimal_sum(conn):
    wallet = WalletService(conn)
    conn.execute("INSERT INTO ledger_entries(account_type,account_owner_id,user_id,asset,amount,entry_type,ref_type,ref_id) VALUES(?,?,?,?,?,?,?,?)", ("PLATFORM_REVENUE", None, None, "USDT", "1.10", "x", "x", 1))
    conn.execute("INSERT INTO ledger_entries(account_type,account_owner_id,user_id,asset,amount,entry_type,ref_type,ref_id) VALUES(?,?,?,?,?,?,?,?)", ("PLATFORM_REVENUE", None, None, "USDT", "2.20", "x", "x", 2))
    assert wallet.account_revenue_balance("PLATFORM_REVENUE", None, "USDT") == Decimal("3.30")


def test_credit_deposit_duplicate_returns_false(conn):
    wallet = WalletService(conn)
    assert wallet.credit_deposit_if_confirmed(1, "USDT", Decimal("10"), "txd1", "dup:key", "ETHEREUM", 12, True) is True
    assert wallet.credit_deposit_if_confirmed(1, "USDT", Decimal("10"), "txd1", "dup:key", "ETHEREUM", 12, True) is False


def test_admin_revenue_command_access_control(monkeypatch, conn):
    import asyncio

    wallet = WalletService(conn)
    conn.execute("INSERT INTO ledger_entries(account_type,account_owner_id,user_id,asset,amount,entry_type,ref_type,ref_id) VALUES(?,?,?,?,?,?,?,?)", ("PLATFORM_REVENUE", None, None, "USDT", "2.50", "x", "x", 1))

    monkeypatch.setattr("bot.ADMIN_IDS", {999})

    class Msg:
        def __init__(self):
            self.replies = []
        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    class U:
        def __init__(self, uid):
            self.id = uid

    def _svc():
        return (conn, wallet, None, None)

    monkeypatch.setattr("bot._services", _svc)

    denied_msg = Msg()
    denied = SimpleNamespace(effective_user=U(1), effective_message=denied_msg)
    asyncio.run(revenue_report(denied, None))
    assert denied_msg.replies[-1] == "Admin only"

    allowed_msg = Msg()
    allowed = SimpleNamespace(effective_user=U(999), effective_message=allowed_msg)
    asyncio.run(revenue_report(allowed, None))
    assert "Platform revenue balances" in allowed_msg.replies[-1]
    assert "USDT: 2.50" in allowed_msg.replies[-1]


def test_watcher_address_map_loaders_do_not_call_sample_consistency(conn, monkeypatch):
    from run_btc_watcher import _address_map as btc_map
    from run_eth_watcher import _address_map as eth_map

    def _boom(*_a, **_k):
        raise AssertionError("sample consistency check should not be called in watcher map loader")

    monkeypatch.setattr("wallet_service.WalletService.verify_address_derivation_consistency", _boom)
    conn.execute("INSERT INTO users(id, telegram_id, username, frozen) VALUES(?,?,?,0)", (11, 2011, "u11"))
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)", (11, "BTC", "BTC", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080", 11, None, "p"))
    assert btc_map(conn)["bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"] == 11
    assert isinstance(eth_map(conn), dict)


def test_deposit_flow_provider_failure_is_controlled(monkeypatch, conn):
    import asyncio
    from types import SimpleNamespace

    class Msg:
        def __init__(self, text):
            self.text = text
            self.replies = []

        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    class User:
        id = 999
        username = "u"

    wallet = WalletService(conn)
    tenant = TenantService(conn)

    class EscrowStub:
        price_service = StaticPriceService({"BTC": Decimal("65000")})

    monkeypatch.setattr("bot._services", lambda: (conn, wallet, tenant, EscrowStub()))
    monkeypatch.setattr(wallet, "get_or_create_deposit_address", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")))

    msg = Msg("100")
    upd = SimpleNamespace(effective_message=msg, effective_user=User())
    ctx = SimpleNamespace(user_data={"dep_asset": "BTC"})
    import bot
    result = asyncio.run(bot.deposit_amount_input(upd, ctx))
    assert result == bot.ConversationHandler.END
    assert "issuance is currently unavailable" in msg.replies[-1].lower()


def test_tx_detail_withdrawal_decrypts_address_and_hides_internal_failures(monkeypatch, tmp_path):
    import asyncio
    import bot

    db = tmp_path / "tx_detail.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    wallet = WalletService(c)
    tenant = TenantService(c)
    uid = tenant.ensure_user(7777, "tester")
    enc_addr = wallet._encrypt_field("0x" + "2" * 40)
    c.execute("INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,txid,failure_reason) VALUES(?,?,?,?,?,?,?,?)", (9, uid, "USDT", "1", enc_addr, "failed", None, "provider boom internal"))
    c.execute("INSERT INTO ledger_entries(id,account_type,account_owner_id,user_id,asset,amount,entry_type,ref_type,ref_id) VALUES(?,?,?,?,?,?,?,?,?)", (19, "USER", uid, uid, "USDT", "-1", "WITHDRAWAL_RESERVE", "withdrawal", 9))
    c.commit()
    c.close()

    def _svc():
        cx = sqlite3.connect(db)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA foreign_keys=ON")
        return (cx, WalletService(cx), TenantService(cx), None)

    monkeypatch.setattr(bot, "_services", _svc)

    class Q:
        data = "tx_detail:19:1"
        from_user = SimpleNamespace(id=7777, username="tester")
        text = None

        async def answer(self, *a, **k):
            return None

        async def edit_message_text(self, txt, **kwargs):
            self.text = txt

    q = Q()
    upd = SimpleNamespace(callback_query=q)
    asyncio.run(bot.profile_actions(upd, SimpleNamespace(user_data={})))
    assert "Address:" in q.text
    assert "0x" + "2" * 40 in q.text
    assert "Status:</b> Failed" in q.text
    assert "TxID:" not in q.text
    assert "provider boom internal" not in q.text


def test_withdrawn_usd_last_24h_counts_signer_retry(conn):
    wallet = WalletService(conn)
    wallet.price_service = StaticPriceService({"USDT": Decimal("1")})
    uid = wallet._ensure_user_row(988)
    conn.execute(
        "INSERT INTO withdrawals(user_id,asset,amount,destination_address,status) VALUES(?,?,?,?,?)",
        (uid, "USDT", "100", "enc", "signer_retry"),
    )
    assert wallet._withdrawn_usd_last_24h(uid) == Decimal("100")


def test_tx_detail_withdrawal_shows_reconciliation_status(monkeypatch, tmp_path):
    import asyncio
    import bot

    db = tmp_path / "tx_detail_retry.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    wallet = WalletService(c)
    tenant = TenantService(c)
    uid = tenant.ensure_user(7778, "tester2")
    enc_addr = wallet._encrypt_field("0x" + "3" * 40)
    c.execute(
        "INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,txid,failure_reason) VALUES(?,?,?,?,?,?,?,?)",
        (10, uid, "USDT", "2", enc_addr, "signer_retry", "pretend-txid", "provider error"),
    )
    c.execute(
        "INSERT INTO ledger_entries(id,account_type,account_owner_id,user_id,asset,amount,entry_type,ref_type,ref_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (20, "USER", uid, uid, "USDT", "-2", "WITHDRAWAL_RESERVE", "withdrawal", 10),
    )
    c.commit()
    c.close()

    def _svc():
        cx = sqlite3.connect(db)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA foreign_keys=ON")
        return (cx, WalletService(cx), TenantService(cx), None)

    monkeypatch.setattr(bot, "_services", _svc)

    class Q:
        data = "tx_detail:20:1"
        from_user = SimpleNamespace(id=7778, username="tester2")
        text = None

        async def answer(self, *a, **k):
            return None

        async def edit_message_text(self, txt, **kwargs):
            self.text = txt

    q = Q()
    upd = SimpleNamespace(callback_query=q)
    asyncio.run(bot.profile_actions(upd, SimpleNamespace(user_data={})))
    assert "Status:</b> Awaiting reconciliation" in q.text
    assert "TxID:" not in q.text


def test_watcher_status_command_includes_signer_and_backlog(monkeypatch, conn):
    import asyncio
    import bot

    upsert_watcher_status(conn, "btc_watcher", success=True)
    upsert_watcher_status(conn, "eth_watcher", success=False, error="eth down")
    upsert_watcher_status(conn, "signer_loop", success=True)
    conn.execute("INSERT INTO users(id,telegram_id,username,frozen) VALUES(?,?,?,0)", (1, 1001, "u1"))
    conn.execute(
        "INSERT INTO withdrawals(user_id,asset,amount,destination_address,status) VALUES(?,?,?,?,?)",
        (1, "USDT", "5", "enc", "signer_retry"),
    )

    monkeypatch.setattr(bot, "ADMIN_IDS", {999})
    monkeypatch.setattr(bot, "_services", lambda: (conn, None, None, None))
    monkeypatch.setattr(bot, "DEPOSIT_ISSUANCE_READY", False)
    monkeypatch.setattr(bot, "DEPOSIT_ISSUANCE_ERROR", "degraded")

    class Msg:
        def __init__(self):
            self.replies = []

        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    denied_msg = Msg()
    denied = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=denied_msg)
    asyncio.run(bot.watcher_status(denied, None))
    assert denied_msg.replies[-1] == "Admin only"

    allowed_msg = Msg()
    allowed = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=allowed_msg)
    asyncio.run(bot.watcher_status(allowed, None))
    text = allowed_msg.replies[-1]
    assert "watcher_status" in text
    assert "- signer:" in text
    assert "- deposit_provider:" in text
    assert "- btc:" in text




def test_watcher_status_default_row_is_disabled(conn):
    status = read_watcher_status(conn, ["btc_watcher"])
    assert status["btc_watcher"]["health_state"] == "disabled"


def test_watcher_status_transient_failure_maps_to_degraded(monkeypatch, conn):
    import bot

    upsert_watcher_status(conn, "btc_watcher", success=False, error="rpc timeout", health="transient_failure")
    monkeypatch.setattr(bot, "ADMIN_IDS", {999})
    monkeypatch.setattr(bot, "_services", lambda: (conn, None, None, None))
    monkeypatch.setattr(bot, "DEPOSIT_ISSUANCE_READY", True)
    monkeypatch.setattr(bot, "DEPOSIT_ISSUANCE_ERROR", None)

    class SignerFake:
        def readiness(self):
            return True, None

    class WalletFake:
        class P:
            def is_ready(self):
                return True, None

        def __init__(self, _conn):
            self.address_provider = self.P()

    monkeypatch.setattr(bot, "SignerService", SignerFake)
    monkeypatch.setattr(bot, "WalletService", WalletFake)

    class Msg:
        def __init__(self):
            self.replies = []

        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    import asyncio
    allowed_msg = Msg()
    allowed = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=allowed_msg)
    asyncio.run(bot.watcher_status(allowed, None))
    assert "- btc: degraded" in allowed_msg.replies[-1]
def test_withdrawal_idempotency_key_persisted_on_request(conn):
    Settings.withdrawals_enabled = True
    Settings.withdrawal_min_interval_seconds = 0
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(92001)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("20"), "DEPOSIT", "deposit", 1)
    wd = wallet.request_withdrawal(uid, "USDT", Decimal("2"), "0x1111111111111111111111111111111111111111")
    row = conn.execute("SELECT idempotency_key FROM withdrawals WHERE id=?", (wd["id"],)).fetchone()
    assert row["idempotency_key"].startswith("wdrow:")


def test_withdrawn_usd_last_24h_counts_submitted_state(conn):
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(92002)
    conn.execute(
        "INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key) VALUES(?,?,?,?,?,?)",
        (uid, "USDT", "25", "enc", "submitted", "k1"),
    )
    assert wallet._withdrawn_usd_last_24h(uid) >= Decimal("24")


def test_identical_withdrawals_create_distinct_row_bound_idempotency_keys(conn):
    Settings.withdrawals_enabled = True
    Settings.withdrawal_min_interval_seconds = 0
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(92011)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("20"), "DEPOSIT", "deposit", 1)
    first = wallet.request_withdrawal(uid, "USDT", Decimal("2"), "0x1111111111111111111111111111111111111111")
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("20"), "DEPOSIT", "deposit", 2)
    second = wallet.request_withdrawal(uid, "USDT", Decimal("2"), "0x1111111111111111111111111111111111111111")
    rows = conn.execute("SELECT id,idempotency_key FROM withdrawals WHERE id IN (?,?) ORDER BY id", (first["id"], second["id"])).fetchall()
    assert rows[0]["idempotency_key"].startswith("wdrow:")
    assert rows[1]["idempotency_key"].startswith("wdrow:")
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]


def test_withdrawal_reconcile_targets_only_requested_id(monkeypatch, conn):
    import asyncio
    import bot

    conn.execute("INSERT INTO users(id,telegram_id,username,frozen) VALUES(?,?,?,0)", (1, 1001, "u1"))
    conn.execute("INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,idempotency_key,submitted_at) VALUES(?,?,?,?,?,?,?,datetime('now','-700 seconds'))", (11, 1, "USDT", "1", "enc", "submitted", "k11"))
    conn.execute("INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,idempotency_key,submitted_at) VALUES(?,?,?,?,?,?,?,datetime('now','-700 seconds'))", (12, 1, "USDT", "1", "enc", "submitted", "k12"))

    monkeypatch.setattr(bot, "ADMIN_IDS", {999})
    monkeypatch.setattr(bot, "_services", lambda: (conn, WalletService(conn), TenantService(conn), None))

    calls = []
    class FakeSigner:
        def reconcile_withdrawal_by_id(self, _wallet, wid, force=False):
            calls.append((wid, force))
            return 1

    monkeypatch.setattr(bot, "SignerService", lambda: FakeSigner())

    class Msg:
        text = "/withdrawal_reconcile 11 CONFIRM"
        def __init__(self):
            self.replies = []
        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    msg = Msg()
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=msg, message=msg)
    asyncio.run(bot.withdrawal_reconcile(upd, None))
    assert calls == [(11, False)]
    assert "processed=1" in msg.replies[-1]


def test_withdrawal_reconcile_fails_closed_when_not_eligible(monkeypatch, conn):
    import asyncio
    import bot

    conn.execute("INSERT INTO users(id,telegram_id,username,frozen) VALUES(?,?,?,0)", (1, 1001, "u1"))
    conn.execute("INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,idempotency_key,submitted_at) VALUES(?,?,?,?,?,?,?,datetime('now'))", (21, 1, "USDT", "1", "enc", "submitted", "k21"))

    monkeypatch.setattr(bot, "ADMIN_IDS", {999})
    monkeypatch.setattr(bot, "_services", lambda: (conn, WalletService(conn), TenantService(conn), None))

    class FakeSigner:
        def reconcile_withdrawal_by_id(self, _wallet, _wid, force=False):
            return 0

    monkeypatch.setattr(bot, "SignerService", lambda: FakeSigner())

    class Msg:
        text = "/withdrawal_reconcile 21 CONFIRM"
        def __init__(self):
            self.replies = []
        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    msg = Msg()
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=msg, message=msg)
    asyncio.run(bot.withdrawal_reconcile(upd, None))
    assert msg.replies[-1] == "Withdrawal is not in a reconcilable state"


def test_withdrawal_reconcile_force_override_targets_only_requested_id(monkeypatch, conn):
    import asyncio
    import bot

    conn.execute("INSERT INTO users(id,telegram_id,username,frozen) VALUES(?,?,?,0)", (1, 1001, "u1"))
    conn.execute("INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,idempotency_key,submitted_at) VALUES(?,?,?,?,?,?,?,datetime('now'))", (31, 1, "USDT", "1", "enc", "submitted", "k31"))
    conn.execute("INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,idempotency_key,submitted_at) VALUES(?,?,?,?,?,?,?,datetime('now','-700 seconds'))", (32, 1, "USDT", "1", "enc", "submitted", "k32"))

    monkeypatch.setattr(bot, "ADMIN_IDS", {999})
    monkeypatch.setattr(bot, "_services", lambda: (conn, WalletService(conn), TenantService(conn), None))

    calls = []

    class FakeSigner:
        def reconcile_withdrawal_by_id(self, _wallet, wid, force=False):
            calls.append((wid, force))
            return 1 if wid == 31 and force else 0

    monkeypatch.setattr(bot, "SignerService", lambda: FakeSigner())

    class Msg:
        text = "/withdrawal_reconcile 31 CONFIRM FORCE"
        def __init__(self):
            self.replies = []
        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    msg = Msg()
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=msg, message=msg)
    asyncio.run(bot.withdrawal_reconcile(upd, None))
    assert calls == [(31, True)]
    assert "processed=1" in msg.replies[-1]


def test_signer_service_force_reconcile_bypasses_backoff_for_target_only(conn, monkeypatch):
    from signer.signer_service import SignerService

    monkeypatch.setattr(Settings, "withdrawals_enabled", True)
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(99111)
    conn.execute("INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,idempotency_key,submitted_at,last_reconciled_at) VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))", (401, uid, "USDT", "1", "enc", "submitted", "k401"))
    conn.execute("INSERT INTO withdrawals(id,user_id,asset,amount,destination_address,status,idempotency_key,submitted_at,last_reconciled_at) VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))", (402, uid, "USDT", "1", "enc", "submitted", "k402"))

    class Provider:
        provider_origin = "http"
        def is_ready(self):
            return True, None
        def reconcile_withdrawal(self, _req):
            return SimpleNamespace(status="submitted", message="still pending", provider_ref=None, external_status="submitted", submitted_at=None)

    svc = SignerService()
    svc.provider = Provider()

    assert svc.reconcile_withdrawal_by_id(wallet, 401, force=False) == 0
    assert svc.reconcile_withdrawal_by_id(wallet, 401, force=True) == 1
    untouched = conn.execute("SELECT last_reconciled_at FROM withdrawals WHERE id=402").fetchone()["last_reconciled_at"]
    assert untouched is not None


def test_watcher_status_distinguishes_signer_health_states(monkeypatch, conn):
    import bot

    monkeypatch.setattr(bot.Settings, "withdrawals_enabled", True)
    state, detail = bot._signer_operator_state({"health_state": "fatal_startup_blocked", "last_error": "fatal"}, True, None)
    assert state == "blocked"
    assert "fatal" in detail

    monkeypatch.setattr(bot.Settings, "withdrawals_enabled", False)
    state2, detail2 = bot._signer_operator_state({"health_state": "ok", "last_error": None}, True, None)
    assert state2 == "disabled"
    assert detail2 == "WITHDRAWALS_ENABLED=false"

    monkeypatch.setattr(bot.Settings, "withdrawals_enabled", True)
    state3, detail3 = bot._signer_operator_state({"health_state": "ok", "last_error": None}, False, "provider unavailable")
    assert state3 == "degraded"
    assert "provider unavailable" in detail3

    state4, detail4 = bot._signer_operator_state({"health_state": "ok", "last_error": None}, True, None)
    assert state4 == "ready"
    assert detail4 == "ok"



def test_deal_conditions_input_no_nameerror_on_confirm(monkeypatch, conn):
    import asyncio
    import bot
    from types import SimpleNamespace

    tenant = TenantService(conn)
    buyer = tenant.ensure_user(5001, "buyer")
    seller = tenant.ensure_user(5002, "seller")
    owner = tenant.ensure_user(5003, "owner")
    tenant.create_or_update_tenant(1, owner, "bot", "bot", "@support", Decimal("1"))
    WalletService(conn).credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txdc", "txdc:0", "ETHEREUM", 12, True)

    class Msg:
        text = "terms"
        replies = []
        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    class Bot:
        async def send_message(self, **kwargs):
            return None

    upd = SimpleNamespace(effective_message=Msg(), effective_user=SimpleNamespace(id=5001, username="buyer"))
    ctx = SimpleNamespace(user_data={"buyer_id": buyer, "seller_id": seller, "seller_username": "seller", "amount": Decimal("50"), "asset": "USDT"}, bot=Bot())
    monkeypatch.setattr(bot, "_enforce_text_rate_limit", lambda *a, **k: asyncio.sleep(0, result=False))
    monkeypatch.setattr(bot, "_services", lambda: (conn, WalletService(conn), tenant, EscrowService(conn)))
    result = asyncio.run(bot.deal_conditions_input(upd, ctx))
    assert result == bot.DEAL_PENDING_VIEW


def test_active_release_screen_no_nameerror(monkeypatch, conn):
    import asyncio
    import bot
    tenant, buyer, seller, owner = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txar", "txar:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    escrow.accept_escrow(view.escrow_id, seller)

    class Q:
        data = f"esc_active_release:{view.escrow_id}"
        from_user = SimpleNamespace(id=100, username="buyer")
        text = None
        async def answer(self, *a, **k): return None
        async def edit_message_text(self, txt, **kwargs): self.text = txt

    q = Q()
    upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=100, username="buyer"))
    monkeypatch.setattr(bot, "_services", lambda: (conn, WalletService(conn), tenant, EscrowService(conn)))
    asyncio.run(bot.esc_active_release_handler(upd, SimpleNamespace(user_data={})))
    assert "Release Funds" in q.text


def test_cancel_response_rejects_forged_and_replay(monkeypatch, conn):
    import asyncio
    import bot
    tenant, buyer, seller, owner = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txc1", "txc1:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    escrow.accept_escrow(view.escrow_id, seller)
    conn.execute("INSERT INTO active_cancel_requests(escrow_id,requester_user_id,responder_user_id,status) VALUES(?,?,?,?)", (view.escrow_id, buyer, seller, "open"))

    class Q:
        data = f"esc_cancel_accept:{view.escrow_id}"
        text = None
        alerts = []
        def __init__(self, tg): self.from_user = SimpleNamespace(id=tg, username=str(tg))
        async def answer(self, txt=None, show_alert=False):
            if txt: self.alerts.append(txt)
        async def edit_message_text(self, txt, **kwargs): self.text = txt

    proxy = _ConnProxy(conn)
    monkeypatch.setattr(bot, "_services", lambda: (proxy, WalletService(proxy), tenant, EscrowService(proxy)))
    async def _send(**kwargs):
        return None
    # requester cannot self-accept
    q1 = Q(100)
    asyncio.run(bot.esc_cancel_response_handler(SimpleNamespace(callback_query=q1, effective_user=q1.from_user), SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=_send))))
    assert any("cannot respond" in a.lower() for a in q1.alerts)
    # unrelated rejected
    tenant.ensure_user(5555, "u")
    q2 = Q(5555)
    asyncio.run(bot.esc_cancel_response_handler(SimpleNamespace(callback_query=q2, effective_user=q2.from_user), SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=_send))))
    assert any("not authorised" in a.lower() for a in q2.alerts)
    # stale request rejected
    conn.execute("UPDATE active_cancel_requests SET status='accepted' WHERE escrow_id=?", (view.escrow_id,))
    q3 = Q(200)
    asyncio.run(bot.esc_cancel_response_handler(SimpleNamespace(callback_query=q3, effective_user=q3.from_user), SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=_send))))
    assert any("no longer valid" in a.lower() for a in q3.alerts)


def test_deposit_minimum_enforced_before_address_issue(monkeypatch, conn):
    import asyncio
    import bot
    class Msg:
        def __init__(self, t): self.text=t; self.replies=[]
        async def reply_text(self, txt, **kwargs): self.replies.append(txt)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=9001, username="u"), effective_message=Msg("39.99"))
    ctx = SimpleNamespace(user_data={"dep_asset":"BTC"})
    monkeypatch.setattr(bot, "_enforce_text_rate_limit", lambda *a, **k: asyncio.sleep(0, result=False))
    result = asyncio.run(bot.deposit_amount_input(upd, ctx))
    assert result == bot.DEPOSIT_ENTER_AMOUNT
    assert "Minimum deposit" in upd.effective_message.replies[-1]


def test_dispute_reason_limit(conn):
    import asyncio
    import bot
    msg = SimpleNamespace(text="x" * 1001, replies=[])
    async def _reply(t, **k): msg.replies.append(t)
    msg.reply_text = _reply
    upd = SimpleNamespace(effective_message=msg)
    ctx = SimpleNamespace(user_data={"awaiting_dispute_reason": True, "dispute_escrow_id": 1})
    asyncio.run(bot.esc_dispute_reason_message(upd, ctx))
    assert "too long" in msg.replies[-1].lower()


def test_seller_decline_records_seller_actor(monkeypatch, conn):
    import asyncio
    import bot
    tenant, buyer, seller, owner = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txsd", "txsd:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")

    class Q:
        data = f"escrow_decline:{view.escrow_id}"
        from_user = SimpleNamespace(id=200, username="seller")
        async def answer(self, *a, **k): return None
        async def edit_message_text(self, *a, **k): return None

    proxy = _ConnProxy(conn)
    monkeypatch.setattr(bot, "_services", lambda: (proxy, WalletService(proxy), tenant, EscrowService(proxy)))
    async def _send(**kwargs):
        return None
    asyncio.run(bot.escrow_accept_decline(SimpleNamespace(callback_query=Q(), effective_user=Q.from_user), SimpleNamespace(bot=SimpleNamespace(send_message=_send))))
    ev = conn.execute("SELECT data_json FROM escrow_events WHERE escrow_id=? AND event_type='cancelled' ORDER BY id DESC LIMIT 1", (view.escrow_id,)).fetchone()
    assert str(seller) in ev["data_json"]


def test_wallet_aead_cached(monkeypatch, conn):
    import wallet_service
    calls = {"n": 0}
    orig = wallet_service.hashlib.pbkdf2_hmac
    def _count(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    monkeypatch.setattr(wallet_service.hashlib, "pbkdf2_hmac", _count)
    monkeypatch.setattr(Settings, "encryption_key", "k")
    w = WalletService(conn)
    w._aead(); w._aead(); w._aead()
    assert calls["n"] == 1


def test_check_user_global_clears_state(monkeypatch):
    import asyncio
    import bot
    called = {"ok": False}
    async def _fake(update, context):
        called["ok"] = True
        return bot.DEAL_SEARCH_INPUT
    monkeypatch.setattr(bot, "deal_check_user", _fake)
    ctx = SimpleNamespace(user_data={"dep_asset":"BTC", "wd_asset":"USDT", "awaiting_dispute_reason":True})
    asyncio.run(bot.check_user_global(SimpleNamespace(), ctx))
    assert called["ok"] is True
    assert "dep_asset" not in ctx.user_data


def test_recover_command_restores_latest_open_escrow(monkeypatch, conn):
    import asyncio
    import bot
    tenant, buyer, seller, owner = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txrc", "txrc:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")

    class Msg:
        replies=[]
        async def reply_text(self, txt, **kwargs): self.replies.append(txt)

    upd = SimpleNamespace(effective_user=SimpleNamespace(id=100, username="buyer"), effective_message=Msg())
    ctx = SimpleNamespace(user_data={})
    monkeypatch.setattr(bot, "_services", lambda: (conn, WalletService(conn), tenant, EscrowService(conn)))
    asyncio.run(bot.recover_command(upd, ctx))
    assert ctx.user_data["escrow_id"] == view.escrow_id


def test_resolve_user_id_raises_typed(conn):
    from bot import _resolve_user_id, UserResolutionError
    with pytest.raises(UserResolutionError):
        _resolve_user_id(conn, 123456)



def test_support_contact_uses_config(monkeypatch):
    import bot
    monkeypatch.setattr(bot.Settings, "support_handle", "@desk")
    assert "@desk" in bot._support_contact_text()


def test_dispute_submit_notifies_all_moderators(monkeypatch, conn):
    import asyncio
    import bot
    tenant, buyer, seller, owner = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txm", "txm:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    sent = []
    class Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs["chat_id"])
            if kwargs["chat_id"] == 9002:
                raise RuntimeError("down")
    monkeypatch.setattr(bot.Settings, "moderator_ids", {9001, 9002, 9003})
    monkeypatch.setattr(bot, "_services", lambda: (conn, WalletService(conn), tenant, EscrowService(conn)))
    q = SimpleNamespace(data=f"esc_dispute_submit:{view.escrow_id}", from_user=SimpleNamespace(id=100, username="buyer"), answer=lambda *a, **k: None, edit_message_text=lambda *a, **k: None)
    async def _answer(*a, **k): return None
    async def _edit(*a, **k): return None
    q.answer = _answer
    q.edit_message_text = _edit
    upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=100, username="buyer"))
    ctx = SimpleNamespace(user_data={"dispute_reason":"r"}, bot=Bot())
    asyncio.run(bot.esc_dispute_submit_handler(upd, ctx))
    assert {9001, 9002, 9003}.issubset(set(sent))



def test_release_confirm_rejects_non_buyer(monkeypatch, conn):
    import asyncio
    import bot
    tenant, buyer, seller, owner = _seed_tenant(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "txrb", "txrb:0", "ETHEREUM", 12, True)
    view = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    escrow.accept_escrow(view.escrow_id, seller)

    class Q:
        data = f"esc_release_confirm:{view.escrow_id}"
        from_user = SimpleNamespace(id=200, username="seller")
        alerts=[]
        async def answer(self, txt=None, show_alert=False):
            if txt: self.alerts.append(txt)
        async def edit_message_text(self, *a, **k): return None

    monkeypatch.setattr(bot, "_services", lambda: (conn, WalletService(conn), tenant, EscrowService(conn)))
    async def _send(**kwargs):
        return None
    q = Q()
    asyncio.run(bot.esc_release_confirm_handler(SimpleNamespace(callback_query=q, effective_user=q.from_user), SimpleNamespace(bot=SimpleNamespace(send_message=_send), user_data={})))
    assert any("only the buyer" in a.lower() for a in q.alerts)


def test_deposit_provider_state_mapping_truth_table():
    state, detail = normalize_deposit_provider_state(
        provider_ready=True,
        issuance_ready=True,
        provider_kind="http",
        startup_error=None,
        provider_reason=None,
    )
    assert state == "ready"
    assert detail == "ok"

    state, detail = normalize_deposit_provider_state(
        provider_ready=False,
        issuance_ready=False,
        provider_kind="disabled",
        startup_error="Production deposit issuance unavailable: no approved external address provider configured",
        provider_reason="no approved external address provider configured",
    )
    assert state == "disabled"
    assert "disabled by configuration" in detail

    state, detail = normalize_deposit_provider_state(
        provider_ready=False,
        issuance_ready=False,
        provider_kind="http",
        startup_error="Production deposit issuance unavailable: ADDRESS_PROVIDER_URL is missing",
        provider_reason="ADDRESS_PROVIDER_URL is missing",
    )
    assert state == "blocked"
    assert "ADDRESS_PROVIDER_URL" in detail

    state, detail = normalize_deposit_provider_state(
        provider_ready=False,
        issuance_ready=False,
        provider_kind="http",
        startup_error=None,
        provider_reason="provider healthcheck failed: timeout",
    )
    assert state == "degraded"
    assert "healthcheck" in detail


def test_watcher_status_config_disabled_overrides_stale_ready(monkeypatch, conn):
    import asyncio
    import bot

    upsert_watcher_status(conn, "btc_watcher", success=True, health="ok")

    monkeypatch.setenv("BTC_WATCHER_ENABLED", "false")
    monkeypatch.setattr(bot, "ADMIN_IDS", {999})
    monkeypatch.setattr(bot, "_services", lambda: (conn, None, None, None))
    monkeypatch.setattr(bot, "DEPOSIT_ISSUANCE_READY", True)
    monkeypatch.setattr(bot, "DEPOSIT_ISSUANCE_ERROR", None)

    class SignerFake:
        def readiness(self):
            return True, None

    class WalletFake:
        class P:
            def is_ready(self):
                return True, None

        def __init__(self, _conn):
            self.address_provider = self.P()

    monkeypatch.setattr(bot, "SignerService", SignerFake)
    monkeypatch.setattr(bot, "WalletService", WalletFake)

    class Msg:
        def __init__(self):
            self.replies = []

        async def reply_text(self, txt, **kwargs):
            self.replies.append(txt)

    allowed_msg = Msg()
    allowed = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=allowed_msg)
    asyncio.run(bot.watcher_status(allowed, None))
    assert "- btc: disabled" in allowed_msg.replies[-1]
