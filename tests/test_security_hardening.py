from decimal import Decimal
import json
import os
import threading
from types import SimpleNamespace

import pytest

from apps.bot_tenant_router.router import TenantNotFoundError, TenantRouter
from config.settings import Settings
from escrow_service import EscrowService
from error_sanitizer import sanitize_runtime_error
from hd_wallet import HDWalletDeriver
from infra.chain_adapters.eth_rpc import EthRpcAdapter
from signer.errors import AmbiguousBroadcastError, DeterministicSigningError
from signer.signer_service import DisabledSignerProvider, SignerService
from tenant_service import TenantService
from wallet_service import WalletService
from watcher_status_service import read_watcher_cursor, write_watcher_cursor
import bot
import run_signer
import run_btc_watcher
import run_eth_watcher
import run_bot
from runtime_preflight import PreflightIntegrityError, PreflightStatus, run_startup_preflight
from watchers.sweep_job import run_once as run_sweep_once


import sqlite3

from infra.db.database import init_db


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    yield c
    c.close()


def _seed(conn):
    tenant = TenantService(conn)
    buyer = tenant.ensure_user(111, "b")
    seller = tenant.ensure_user(222, "s")
    owner = tenant.ensure_user(333, "o")
    tenant.create_or_update_tenant(1, owner, "bot", "bot", "@support", Decimal("2"))
    return buyer, seller


def test_private_derivation_disabled(monkeypatch):
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ab" * 32)
    d = HDWalletDeriver()
    with pytest.raises(RuntimeError):
        d.derive_btc(1)


def test_production_requires_xpub_not_seed(monkeypatch):
    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ab" * 32)
    d = HDWalletDeriver()
    with pytest.raises(RuntimeError):
        d.derive_btc_address(1)


def test_signer_disabled_provider_fails_closed():
    with pytest.raises(Exception):
        DisabledSignerProvider().sign_and_broadcast("ETH", "0x" + "1" * 40, "1")


def test_sweep_job_disabled():
    with pytest.raises(RuntimeError):
        run_sweep_once()


def test_resolve_dispute_requires_authorized_moderator(conn, monkeypatch):
    monkeypatch.setattr(Settings, "moderator_ids", {999})
    buyer, seller = _seed(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "tx", "tx:0", "ETHEREUM", 12, True)
    e = escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "deal")
    escrow.accept_escrow(e.escrow_id, seller)
    escrow.dispute(e.escrow_id, buyer, "issue")
    with pytest.raises(PermissionError):
        escrow.resolve_dispute(e.escrow_id, 123, "refund_buyer")


def test_create_escrow_description_max_len(conn):
    buyer, seller = _seed(conn)
    escrow = EscrowService(conn)
    escrow.wallet_service.credit_deposit_if_confirmed(buyer, "USDT", Decimal("100"), "tx2", "tx2:0", "ETHEREUM", 12, True)
    with pytest.raises(ValueError):
        escrow.create_escrow(1, buyer, seller, "USDT", Decimal("50"), "x" * 501)


def test_withdrawals_blocked_when_disabled(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", False)
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(9001)
    with pytest.raises(ValueError):
        wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")


def test_monitored_address_map_decrypts_encrypted_rows(conn, monkeypatch):
    monkeypatch.setattr(Settings, "encryption_key", "k" * 32)
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(7001)
    enc_addr = wallet._encrypt_field("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080")
    conn.execute(
        "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)",
        (uid, "BTC", "BTC", enc_addr, uid, None, None),
    )
    result = wallet.monitored_deposit_address_map(["BTC"])
    assert result == {"bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080": uid}


def test_kdf_backward_compatible_decrypt(monkeypatch, conn):
    import base64

    monkeypatch.setattr(Settings, "encryption_key", "legacy-key")
    wallet = WalletService(conn)
    nonce = b"0" * 12
    ct = wallet._legacy_aead().encrypt(nonce, b"legacy-value", None)
    legacy = "enc:" + base64.b64encode(nonce + ct).decode()
    assert wallet._decrypt_field(legacy) == "legacy-value"
    assert wallet._decrypt_field(wallet._encrypt_field("new-value")) == "new-value"


def test_eth_native_scan_uses_blocks_and_persists_cursor(conn, monkeypatch):
    write_watcher_cursor(conn, "eth_watcher", 100)
    calls = []

    def fake_rpc(method, params):
        calls.append(method)
        if method == "eth_blockNumber":
            return {"result": hex(116)}
        if method == "eth_getBlockByNumber":
            bn = int(params[0], 16)
            return {"result": {"transactions": [{"to": "0x1111111111111111111111111111111111111111", "value": hex(10**18), "hash": f"0x{bn:064x}"}]}}
        if method == "eth_getLogs":
            return {"result": []}
        return {"result": None}

    monkeypatch.setenv("ETH_RPC_URL", "http://example.invalid")
    adapter = EthRpcAdapter({"0x1111111111111111111111111111111111111111": 1}, conn=conn)
    monkeypatch.setattr(adapter, "_rpc", fake_rpc)
    deposits, finalized = adapter.fetch_deposits()
    assert deposits and deposits[0].asset == "ETH"
    assert finalized == 104
    assert "eth_getBlockByNumber" in calls


def test_eth_cursor_floor_from_db(conn):
    write_watcher_cursor(conn, "eth_watcher", 123)
    assert read_watcher_cursor(conn, "eth_watcher") == 123


def test_tenant_router_resolves_and_unknown(conn):
    tenant = TenantService(conn)
    owner = tenant.ensure_user(1, "owner")
    tenant.create_or_update_tenant(77, owner, "mybot", "mybot", "@support", Decimal("1"))
    router = TenantRouter(conn)
    ctx = router.resolve_tenant("@mybot")
    assert ctx.tenant_bot_id == 77
    with pytest.raises(TenantNotFoundError):
        router.resolve_tenant("@missing")


def test_signer_service_skips_pending_when_withdrawals_disabled(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", False)
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(55)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("10"), "DEPOSIT", "deposit", 1)
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status) VALUES(?,?,?,?,?)", (uid, "USDT", "1", wallet._encrypt_field("0x1111111111111111111111111111111111111111"), "pending"))
    assert SignerService().process_pending_withdrawals(wallet) == 0
    row = conn.execute("SELECT status FROM withdrawals WHERE user_id=?", (uid,)).fetchone()
    assert row["status"] == "pending"


def test_xpub_configuration_fails_closed(monkeypatch):
    monkeypatch.setattr(Settings, "btc_xpub", "xpub-test")
    monkeypatch.setattr(Settings, "ltc_xpub", "")
    monkeypatch.setattr(Settings, "eth_xpub", "")
    d = HDWalletDeriver()
    with pytest.raises(RuntimeError, match="hardened derivation"):
        d.validate_xpub_configuration()

def test_derivation_mismatch_detection_fails_closed(conn, monkeypatch):
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(99)
    conn.execute(
        "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)",
        (uid, "BTC", "BTC", wallet._encrypt_field("bc1qwrong"), uid, None, wallet._encrypt_field("m/84'/0'/99'/0/0")),
    )

    class A:
        public_address = "bc1qexpected"

    monkeypatch.setattr(wallet.hd, "derive_btc_address", lambda _uid: A())
    with pytest.raises(RuntimeError):
        wallet.verify_address_derivation_consistency()


def test_eth_chunk_limit_advances_cursor_gradually(conn, monkeypatch):
    write_watcher_cursor(conn, "eth_watcher", 100)
    monkeypatch.setattr(Settings, "eth_max_blocks_per_run", 2)

    def fake_rpc(method, params):
        if method == "eth_blockNumber":
            return {"result": hex(120)}
        if method == "eth_getBlockByNumber":
            bn = int(params[0], 16)
            return {"result": {"transactions": [{"to": "0x1111111111111111111111111111111111111111", "value": hex(10**18), "hash": f"0x{bn:064x}"}]}}
        if method == "eth_getLogs":
            return {"result": []}
        return {"result": None}

    monkeypatch.setenv("ETH_RPC_URL", "http://example.invalid")
    adapter = EthRpcAdapter({"0x1111111111111111111111111111111111111111": 1}, conn=conn)
    monkeypatch.setattr(adapter, "_rpc", fake_rpc)
    _, finalized = adapter.fetch_deposits()
    assert finalized == 102




def test_derivation_mismatch_full_scan_catches_old_rows(conn, monkeypatch):
    wallet = WalletService(conn)

    class A:
        public_address = "bc1qexpected"

    monkeypatch.setattr(wallet.hd, "derive_btc_address", lambda _uid: A())
    for i in range(30):
        uid = wallet._ensure_user_row(1000 + i)
        addr = "bc1qexpected" if i != 0 else "bc1qwrong"
        conn.execute(
            "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)",
            (uid, "BTC", "BTC", wallet._encrypt_field(addr), uid, None, wallet._encrypt_field(f"m/84'/0'/{uid}'/0/0")),
        )
    with pytest.raises(RuntimeError):
        wallet.verify_address_derivation_consistency(sample_size=None)


def test_preflight_runs_derivation_check_before_bot_polling(monkeypatch):
    called = []

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")

    def _preflight(_service):
        called.append("preflight")
        return PreflightStatus(service_name="bot", deposit_issuance_ready=False, deposit_issuance_error="down")

    monkeypatch.setattr(bot, "run_startup_preflight", _preflight)

    class DummyApp:
        def add_handler(self, *_a, **_k):
            return None

        def run_polling(self):
            called.append("polling")

    class DummyBuilder:
        def token(self, _t):
            return self

        def build(self):
            return DummyApp()

    monkeypatch.setattr(bot.Application, "builder", staticmethod(lambda: DummyBuilder()))
    bot.main()
    assert called[0] == "preflight"
    assert "polling" in called


def test_signer_startup_preflight_called(monkeypatch):
    called = []
    monkeypatch.setattr(run_signer, "run_startup_preflight", lambda _: called.append("preflight"))
    monkeypatch.setattr(run_signer, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        run_signer.main()
    assert called == ["preflight"]


def test_preflight_initializes_db_and_runs_consistency(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "db.sqlite3"))
    calls = []
    original = WalletService.verify_address_derivation_consistency

    def _spy(self, sample_size=25):
        calls.append(sample_size)
        return original(self, sample_size)

    monkeypatch.setattr(WalletService, "verify_address_derivation_consistency", _spy)
    run_startup_preflight("test")
    assert calls == [None]




def test_preflight_route_integrity_failure_is_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "fatal.sqlite3"))
    original = WalletService.ensure_wallet_route_integrity

    def _boom(self):
        raise RuntimeError("tampered routes")

    monkeypatch.setattr(WalletService, "ensure_wallet_route_integrity", _boom)
    with pytest.raises(PreflightIntegrityError):
        run_startup_preflight("bot")
    with pytest.raises(PreflightIntegrityError):
        run_startup_preflight("btc_watcher")
    with pytest.raises(PreflightIntegrityError):
        run_startup_preflight("eth_watcher")
    with pytest.raises(PreflightIntegrityError):
        run_startup_preflight("signer")
    monkeypatch.setattr(WalletService, "ensure_wallet_route_integrity", original)
def test_docs_remove_unsupported_assets_and_legacy_entrypoint_clean():
    banned = [("US"+"DC"), ("SO"+"L"), ("XR"+"P")]
    for path in ("README.md", "docs/RUNBOOK.md", "docs/ARCHITECTURE.md", "apps/bot_main/main.py"):
        text = open(path, "r", encoding="utf-8").read()
        for asset in banned:
            assert asset not in text


def test_db_constraints_reject_invalid_assets_and_username(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)",
            (1, "DOGE", "BTC", "addr", 1, None, None),
        )
    conn.execute("INSERT INTO users(telegram_id, username, frozen) VALUES(?,?,0)", (9090, "owner"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bots(id, owner_user_id, bot_extra_fee_percent, display_name, telegram_username) VALUES(?,?,?,?,?)",
            (2, 1, "0", "EscrowHub", "@BadName"),
        )

def test_agents_line_exactly_once():
    text = open("AGENTS.md", "r", encoding="utf-8").read()
    line = "When you find a security vunerabilty, flag it immediately with a WARNING comment and suggest a secure alternative. Never implement insecure patters even if asked."
    assert text.count(line) == 1


def test_preflight_reports_bot_degraded_mode_when_deposit_issuance_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "prod.sqlite3"))
    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setattr(Settings, "ltc_xpub", "")
    monkeypatch.setattr(Settings, "eth_xpub", "")
    status = run_startup_preflight("bot")
    assert status.deposit_issuance_ready is False
    assert "Production deposit issuance unavailable" in (status.deposit_issuance_error or "")


def test_watcher_entrypoints_run_full_startup_preflight(monkeypatch):
    calls = []
    monkeypatch.setattr(run_btc_watcher, "run_startup_preflight", lambda s: calls.append(("btc", s)))
    monkeypatch.setattr(run_eth_watcher, "run_startup_preflight", lambda s: calls.append(("eth", s)))
    monkeypatch.setenv("BTC_WATCHER_ENABLED", "false")
    monkeypatch.setenv("ETH_WATCHER_ENABLED", "false")
    run_btc_watcher.main()
    run_eth_watcher.main()
    assert ("btc", "btc_watcher") not in calls
    monkeypatch.setenv("BTC_WATCHER_ENABLED", "true")
    monkeypatch.setenv("ETH_WATCHER_ENABLED", "true")
    monkeypatch.setattr(run_btc_watcher, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("stop-btc")))
    monkeypatch.setattr(run_eth_watcher, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("stop-eth")))
    with pytest.raises(RuntimeError, match="stop-btc"):
        run_btc_watcher.main()
    with pytest.raises(RuntimeError, match="stop-eth"):
        run_eth_watcher.main()
    assert ("btc", "btc_watcher") in calls
    assert ("eth", "eth_watcher") in calls


def test_db_username_normalization_collision_fails_clearly(tmp_path):
    db = tmp_path / "collision.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("CREATE TABLE bots (id INTEGER PRIMARY KEY, owner_user_id INTEGER NOT NULL, bot_extra_fee_percent TEXT NOT NULL DEFAULT '0', support_contact TEXT, display_name TEXT NOT NULL, telegram_username TEXT, created_at TEXT)")
    c.execute("INSERT INTO bots(id, owner_user_id, bot_extra_fee_percent, display_name, telegram_username) VALUES(?,?,?,?,?)", (1, 1, "0", "A", "Foo"))
    c.execute("INSERT INTO bots(id, owner_user_id, bot_extra_fee_percent, display_name, telegram_username) VALUES(?,?,?,?,?)", (2, 1, "0", "B", "@foo"))
    with pytest.raises(RuntimeError, match="normalization collision"):
        init_db(c)
    c.close()


def test_db_rejects_invalid_asset_chain_family_combo(conn):
    with pytest.raises(sqlite3.IntegrityError, match="asset/chain_family"):
        conn.execute(
            "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)",
            (1, "USDT", "BTC", "bad", 1, None, None),
        )


def test_withdrawal_failure_reason_stored_without_txid(conn):
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(4242)
    conn.execute(
        "INSERT INTO withdrawals(user_id,asset,amount,destination_address,status) VALUES(?,?,?,?,?)",
        (uid, "USDT", "1", wallet._encrypt_field("0x" + "1" * 40), "pending"),
    )
    wid = conn.execute("SELECT id FROM withdrawals WHERE user_id=?", (uid,)).fetchone()["id"]
    wallet.mark_withdrawal_failed(int(wid), "provider internals: boom")
    row = conn.execute("SELECT status, txid, failure_reason FROM withdrawals WHERE id=?", (wid,)).fetchone()
    assert row["status"] == "failed"
    assert row["txid"] is None
    assert "provider internals" in row["failure_reason"]


def test_production_settings_require_sqlite_db_path():
    import subprocess
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env.pop("SQLITE_DB_PATH", None)
    result = subprocess.run(["python", "-c", "import config.settings"], capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "SQLITE_DB_PATH is required in production" in (result.stderr + result.stdout)


def test_watcher_and_signer_preflight_does_not_require_deposit_issuance(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setattr(Settings, "ltc_xpub", "")
    monkeypatch.setattr(Settings, "eth_xpub", "")
    run_startup_preflight("btc_watcher")
    run_startup_preflight("eth_watcher")
    run_startup_preflight("signer")


def test_legacy_bot_entrypoint_blocked_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_LEGACY_BOT_MAIN", "true")
    import importlib.util
    spec = importlib.util.spec_from_file_location("legacy_main_mod", os.path.join("apps", "bot_main", "main.py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    with pytest.raises(ImportError):
        spec.loader.exec_module(mod)
    text = open("apps/bot_main/main.py", "r", encoding="utf-8").read()
    assert "Use run_bot.py" in text


def test_signer_ambiguous_error_moves_to_retry_without_releasing(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", True)

    class TimeoutProvider:
        def sign_and_broadcast(self, *_a, **_k):
            raise RuntimeError("network timeout")

    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(12345)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("10"), "DEPOSIT", "deposit", 1)
    req = wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")

    svc = SignerService()
    svc.provider = TimeoutProvider()
    assert svc.process_pending_withdrawals(wallet) == 0

    row = conn.execute("SELECT status FROM withdrawals WHERE id=?", (req["id"],)).fetchone()
    assert row["status"] == "signer_retry"
    assert wallet.available_balance(uid, "USDT") == Decimal("9")


def test_profile_deposit_fails_closed_when_issuance_degraded(monkeypatch):
    import asyncio
    import bot

    monkeypatch.setattr(bot, "DEPOSIT_ISSUANCE_READY", False)

    class Q:
        data = "profile_deposit"
        from_user = SimpleNamespace(id=42, username="u")
        text = None

        async def answer(self, *a, **k):
            return None

        async def edit_message_text(self, txt, **kwargs):
            self.text = txt

    q = Q()
    upd = SimpleNamespace(callback_query=q)
    asyncio.run(bot.profile_actions(upd, SimpleNamespace(user_data={})))
    assert "issuance is currently unavailable" in (q.text or "").lower()


def test_deposit_select_asset_fails_closed_when_issuance_degraded(monkeypatch):
    import asyncio
    import bot

    monkeypatch.setattr(bot, "DEPOSIT_ISSUANCE_READY", False)

    class Q:
        data = "dep_asset:BTC"
        text = None

        async def answer(self, *a, **k):
            return None

        async def edit_message_text(self, txt, **kwargs):
            self.text = txt

    q = Q()
    upd = SimpleNamespace(callback_query=q)
    result = asyncio.run(bot.deposit_select_asset(upd, SimpleNamespace(user_data={})))
    assert result == bot.ConversationHandler.END
    assert "issuance is currently unavailable" in (q.text or "").lower()


def test_signer_typed_ambiguous_goes_to_retry(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", True)

    class Provider:
        def sign_and_broadcast(self, *_a, **_k):
            raise AmbiguousBroadcastError("network uncertain")

    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(70001)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("10"), "DEPOSIT", "deposit", 1)
    req = wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")
    svc = SignerService()
    svc.provider = Provider()
    svc.process_pending_withdrawals(wallet)
    row = conn.execute("SELECT status FROM withdrawals WHERE id=?", (req["id"],)).fetchone()
    assert row["status"] == "signer_retry"


def test_signer_typed_deterministic_goes_failed(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", True)

    class Provider:
        def sign_and_broadcast(self, *_a, **_k):
            raise DeterministicSigningError("invalid nonce")

    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(70002)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("10"), "DEPOSIT", "deposit", 1)
    req = wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")
    svc = SignerService()
    svc.provider = Provider()
    svc.process_pending_withdrawals(wallet)
    row = conn.execute("SELECT status FROM withdrawals WHERE id=?", (req["id"],)).fetchone()
    assert row["status"] == "failed"


def test_signer_unknown_defaults_to_retry(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", True)

    class Provider:
        def sign_and_broadcast(self, *_a, **_k):
            raise RuntimeError("unexpected")

    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(70003)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("10"), "DEPOSIT", "deposit", 1)
    req = wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")
    svc = SignerService()
    svc.provider = Provider()
    svc.process_pending_withdrawals(wallet)
    row = conn.execute("SELECT status FROM withdrawals WHERE id=?", (req["id"],)).fetchone()
    assert row["status"] == "signer_retry"


def test_withdrawals_rebuild_migration_preserves_fk_and_index(tmp_path):
    db = tmp_path / "old_withdrawals.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER UNIQUE NOT NULL, username TEXT, frozen INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, asset TEXT NOT NULL CHECK(asset IN ('BTC','LTC','ETH','USDT')), amount TEXT NOT NULL, destination_address TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','broadcasted','failed')), txid TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    init_db(c)
    sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='withdrawals'").fetchone()["sql"]
    assert "signer_retry" in sql
    cols = {row["name"] for row in c.execute("PRAGMA table_info(withdrawals)").fetchall()}
    assert "failure_reason" in cols
    fks = c.execute("PRAGMA foreign_key_list(withdrawals)").fetchall()
    assert any(row["from"] == "user_id" and row["table"] == "users" for row in fks)
    idx = c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_withdrawals_user_status_created'").fetchone()
    assert idx is not None
    c.close()


def test_production_deposit_issuance_uses_provider_and_persists_metadata(conn, monkeypatch):
    from address_provider import IssuedAddress

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setattr(Settings, "encryption_key", "k" * 32)

    class Provider:
        def is_ready(self):
            return True, None

        def get_or_create_address(self, user_id, asset):
            assert asset in {"BTC", "LTC", "ETH", "USDT"}
            return IssuedAddress(address="0x" + "2" * 40, provider_origin="external_http", provider_ref=f"route:{user_id}:{asset}")

    wallet = WalletService(conn)
    wallet.address_provider = Provider()
    route = wallet.get_or_create_deposit_address(5555, "USDT")
    assert route.address.startswith("0x")
    row = conn.execute("SELECT provider_origin, provider_ref, derivation_path FROM wallet_addresses WHERE asset='USDT'").fetchone()
    assert row["provider_origin"] == "external_http"
    assert "route:" in row["provider_ref"]
    assert row["derivation_path"] is None


def test_production_deposit_issuance_fails_closed_without_provider(conn, monkeypatch):
    monkeypatch.setattr(Settings, "is_production", True)
    wallet = WalletService(conn)
    with pytest.raises(RuntimeError):
        wallet.get_or_create_deposit_address(9999, "BTC")


def test_provider_issued_invalid_addresses_rejected(conn, monkeypatch):
    from address_provider import IssuedAddress

    monkeypatch.setattr(Settings, "is_production", True)

    invalid_by_asset = {
        "BTC": "not-btc",
        "LTC": "not-ltc",
        "ETH": "0x1234",
        "USDT": "0x1234",
    }

    class Provider:
        def is_ready(self):
            return True, None

        def get_or_create_address(self, user_id, asset):
            return IssuedAddress(address=invalid_by_asset[asset], provider_origin="external_http", provider_ref=f"route:{user_id}:{asset}")

    wallet = WalletService(conn)
    wallet.address_provider = Provider()
    for asset in ("BTC", "LTC", "ETH", "USDT"):
        with pytest.raises(RuntimeError, match="invalid"):
            wallet.get_or_create_deposit_address(42000 + len(asset), asset)


def test_wallet_address_guards_allow_same_user_evm_reuse(conn):
    conn.execute(
        "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)",
        (1, "ETH", "ETHEREUM", "0xabc", _fp("ETHEREUM", "0xabc"), "external_http", "route:1:ETH"),
    )
    conn.execute(
        "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)",
        (1, "USDT", "ETHEREUM", "0xabc", _fp("ETHEREUM", "0xabc"), "external_http", "route:1:USDT"),
    )


def test_wallet_address_guards_reject_cross_user_chain_address_reuse(conn):
    conn.execute(
        "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)",
        (1, "ETH", "ETHEREUM", "0xdef", _fp("ETHEREUM", "0xdef"), "external_http", "route:1:ETH"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="fingerprint"):
        conn.execute(
            "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)",
            (2, "USDT", "ETHEREUM", "0xdef", _fp("ETHEREUM", "0xdef"), "external_http", "route:2:USDT"),
        )


def test_wallet_address_guards_reject_provider_ref_rebinding(conn):
    conn.execute(
        "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)",
        (1, "BTC", "BTC", "bc1qa", _fp("BTC", "bc1qa"), "external_http", "route:shared"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="provider_ref"):
        conn.execute(
            "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)",
            (1, "LTC", "LTC", "ltc1qa", _fp("LTC", "ltc1qa"), "external_http", "route:shared"),
        )


def test_deposit_issuance_is_idempotent_for_racing_first_requests(tmp_path, monkeypatch):
    from address_provider import IssuedAddress

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setattr(Settings, "encryption_key", "k" * 32)
    db = tmp_path / "race.db"
    bootstrap = sqlite3.connect(db)
    bootstrap.row_factory = sqlite3.Row
    init_db(bootstrap)
    bootstrap.execute("INSERT INTO users(telegram_id, username, frozen) VALUES(?,?,0)", (9090, None))
    bootstrap.commit()
    bootstrap.close()

    class Provider:
        def is_ready(self):
            return True, None

        def get_or_create_address(self, user_id, asset):
            return IssuedAddress(address="0x" + "4" * 40, provider_origin="external_http", provider_ref=f"route:{user_id}:{asset}")

    routes = []

    def _run():
        conn = sqlite3.connect(db, timeout=10)
        conn.row_factory = sqlite3.Row
        wallet = WalletService(conn)
        wallet.address_provider = Provider()
        routes.append(wallet.get_or_create_deposit_address(9090, "USDT").address)
        conn.close()

    t1 = threading.Thread(target=_run)
    t2 = threading.Thread(target=_run)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert routes == ["0x" + "4" * 40, "0x" + "4" * 40]
    verify = sqlite3.connect(db)
    verify.row_factory = sqlite3.Row
    rows = verify.execute("SELECT COUNT(*) AS c FROM wallet_addresses WHERE user_id=(SELECT id FROM users WHERE telegram_id=9090) AND asset='USDT'").fetchone()
    assert rows["c"] == 1
    verify.close()


def test_http_provider_production_requires_https_and_token(monkeypatch):
    from address_provider import HttpAddressProvider

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("ADDRESS_PROVIDER_URL", "http://insecure.example")
    monkeypatch.setenv("ADDRESS_PROVIDER_TOKEN", "")
    provider = HttpAddressProvider()
    ready, err = provider.is_ready()
    assert ready is False
    assert "https" in (err or "")


def test_http_provider_health_requires_explicit_ready_true(monkeypatch):
    import io
    from address_provider import HttpAddressProvider
    import address_provider

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("ADDRESS_PROVIDER_URL", "https://provider.example")
    monkeypatch.setenv("ADDRESS_PROVIDER_TOKEN", "token")

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return io.BytesIO(b'{"ok": true}').read()

    monkeypatch.setattr(address_provider, "urlopen", lambda *_a, **_k: Resp())
    provider = HttpAddressProvider()
    ready, err = provider.is_ready()
    assert ready is False
    assert "not ready" in (err or "")


def test_http_provider_production_requires_token(monkeypatch):
    from address_provider import HttpAddressProvider

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("ADDRESS_PROVIDER_URL", "https://secure.example")
    monkeypatch.setenv("ADDRESS_PROVIDER_TOKEN", "")
    provider = HttpAddressProvider()
    ready, err = provider.is_ready()
    assert ready is False
    assert "TOKEN" in (err or "")


def test_http_provider_health_malformed_response_rejected(monkeypatch):
    from address_provider import HttpAddressProvider
    import address_provider

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("ADDRESS_PROVIDER_URL", "https://provider.example")
    monkeypatch.setenv("ADDRESS_PROVIDER_TOKEN", "token")

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b"not-json"

    monkeypatch.setattr(address_provider, "urlopen", lambda *_a, **_k: Resp())
    provider = HttpAddressProvider()
    ready, err = provider.is_ready()
    assert ready is False
    assert "healthcheck failed" in (err or "")


def _fp(chain_family: str, address: str) -> str:
    import hashlib
    return hashlib.sha256(f"{chain_family}:{address}".encode()).hexdigest()


def test_encrypted_duplicate_plaintext_backfill_detected(conn, monkeypatch):
    monkeypatch.setattr(Settings, "encryption_key", "k" * 32)
    w = WalletService(conn)
    a = w._encrypt_field("0x1111111111111111111111111111111111111111")
    b = w._encrypt_field("0x1111111111111111111111111111111111111111")
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,provider_origin,provider_ref) VALUES(?,?,?,?,?,?)", (1, "ETH", "ETHEREUM", a, "external_http", "r1"))
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,provider_origin,provider_ref) VALUES(?,?,?,?,?,?)", (2, "USDT", "ETHEREUM", b, "external_http", "r2"))
    with pytest.raises(RuntimeError, match="fingerprint collision"):
        w.ensure_wallet_route_integrity()


def test_monitored_map_fails_closed_on_duplicate_decrypted_route(conn, monkeypatch):
    monkeypatch.setattr(Settings, "encryption_key", "k" * 32)
    w = WalletService(conn)
    a = w._encrypt_field("0x2222222222222222222222222222222222222222")
    b = w._encrypt_field("0x2222222222222222222222222222222222222222")
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,provider_origin,provider_ref) VALUES(?,?,?,?,?,?)", (1, "ETH", "ETHEREUM", a, "external_http", "ra"))
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,provider_origin,provider_ref) VALUES(?,?,?,?,?,?)", (2, "USDT", "ETHEREUM", b, "external_http", "rb"))
    with pytest.raises(RuntimeError, match="duplicate monitored deposit route"):
        w.monitored_deposit_address_map(["ETH", "USDT"])


def test_wallet_address_guards_use_fingerprint_not_ciphertext(conn):
    a1 = "enc:AAAA"
    a2 = "enc:BBBB"
    fp = _fp("BTC", "bc1qzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)", (1, "BTC", "BTC", a1, fp, "external_http", "x1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)", (2, "BTC", "BTC", a2, fp, "external_http", "x2"))


def test_provider_rebinding_blocked_by_fingerprint(conn):
    fp1 = _fp("ETHEREUM", "0x3333333333333333333333333333333333333333")
    fp2 = _fp("ETHEREUM", "0x4444444444444444444444444444444444444444")
    conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)", (1, "ETH", "ETHEREUM", "a", fp1, "external_http", "shared"))
    with pytest.raises(sqlite3.IntegrityError, match="provider_ref"):
        conn.execute("INSERT INTO wallet_addresses(user_id,asset,chain_family,address,address_fingerprint,provider_origin,provider_ref) VALUES(?,?,?,?,?,?,?)", (1, "USDT", "ETHEREUM", "b", fp2, "external_http", "shared"))


def test_http_provider_health_requires_supported_coverage(monkeypatch):
    import io
    import address_provider
    from address_provider import HttpAddressProvider

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("ADDRESS_PROVIDER_URL", "https://provider.example")
    monkeypatch.setenv("ADDRESS_PROVIDER_TOKEN", "token")

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def read(self): return io.BytesIO(b'{"ready": true}').read()

    monkeypatch.setattr(address_provider, "urlopen", lambda *_a, **_k: Resp())
    ready, err = HttpAddressProvider().is_ready()
    assert ready is False
    assert "supported_assets" in (err or "") or "supported_chain_families" in (err or "")


def test_http_provider_rejects_asset_chain_mismatch(monkeypatch):
    import io
    import address_provider
    from address_provider import HttpAddressProvider

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("ADDRESS_PROVIDER_URL", "https://provider.example")
    monkeypatch.setenv("ADDRESS_PROVIDER_TOKEN", "token")

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def read(self): return io.BytesIO(b'{"address":"0x1111111111111111111111111111111111111111","provider_ref":"r","asset":"BTC","chain_family":"BTC"}').read()

    monkeypatch.setattr(address_provider, "urlopen", lambda *_a, **_k: Resp())
    with pytest.raises(RuntimeError, match="asset mismatch"):
        HttpAddressProvider().get_or_create_address(1, "ETH")


def test_wallet_migration_constraints_present_after_init(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(wallet_addresses)").fetchall()}
    assert "address_fingerprint" in cols
    idx1 = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_wallet_addresses_chain_fingerprint'").fetchone()
    idx2 = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_wallet_addresses_provider_ref'").fetchone()
    assert idx1 is not None
    assert idx2 is not None


def test_agents_sentence_once():
    text = open("AGENTS.md", "r", encoding="utf-8").read()
    needle = 'When you find a security vunerabilty, flag it immediately with a WARNING comment and suggest a secure alternative. Never implement insecure patters even if asked.'
    assert text.count(needle) == 1


def test_withdrawal_provider_requires_https_and_token_in_production(monkeypatch):
    from signer.withdrawal_provider import HttpWithdrawalProvider

    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setenv("WITHDRAWAL_PROVIDER_URL", "http://provider.example")
    monkeypatch.delenv("WITHDRAWAL_PROVIDER_TOKEN", raising=False)
    ready, err = HttpWithdrawalProvider().is_ready()
    assert ready is False
    assert "https://" in (err or "")


def test_withdrawal_provider_malformed_response_fails_closed(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", True)
    monkeypatch.setattr(Settings, "withdrawal_min_interval_seconds", 0)
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(91001)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("10"), "DEPOSIT", "deposit", 1)
    req = wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")

    class Provider:
        provider_origin = "external_http"

        def is_ready(self):
            return True, None

        def execute_withdrawal(self, _req):
            raise RuntimeError("withdrawal provider returned malformed JSON")

        def reconcile_withdrawal(self, _req):
            raise RuntimeError("bad payload")

    svc = SignerService()
    svc.provider = Provider()
    svc.process_withdrawals(wallet)
    row = conn.execute("SELECT status FROM withdrawals WHERE id=?", (req["id"],)).fetchone()
    assert row["status"] == "signer_retry"


def test_withdrawal_rebuild_migration_preserves_new_columns_and_indexes(tmp_path):
    db = tmp_path / "old_withdrawals_new.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER UNIQUE NOT NULL, username TEXT, frozen INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, asset TEXT NOT NULL CHECK(asset IN ('BTC','LTC','ETH','USDT')), amount TEXT NOT NULL, destination_address TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','broadcasted','failed')), txid TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    init_db(c)
    cols = {row["name"] for row in c.execute("PRAGMA table_info(withdrawals)").fetchall()}
    assert "provider_origin" in cols
    assert "provider_ref" in cols
    assert "idempotency_key" in cols
    idx = c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_withdrawals_idempotency_key'").fetchone()
    assert idx is not None
    c.close()


def test_withdrawal_provider_ref_rebinding_blocked(conn):
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(93001)
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,provider_origin,provider_ref,idempotency_key) VALUES(?,?,?,?,?,?,?,?)", (uid, "USDT", "1", "a", "submitted", "external_http", "pref:1", "k1"))
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key) VALUES(?,?,?,?,?,?)", (uid, "USDT", "1", "b", "pending", "k2"))
    with pytest.raises(sqlite3.IntegrityError, match="provider_ref"):
        conn.execute("UPDATE withdrawals SET provider_origin='external_http', provider_ref='pref:1' WHERE id=2")


def test_withdrawal_idempotency_rebinding_blocked(conn):
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(93002)
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key) VALUES(?,?,?,?,?,?)", (uid, "USDT", "1", "a", "pending", "idem:1"))
    with pytest.raises(sqlite3.IntegrityError, match="idempotency_key"):
        conn.execute("UPDATE withdrawals SET idempotency_key='idem:2' WHERE id=1")


def test_signer_reconcile_provider_ref_drift_fails_closed(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", True)
    monkeypatch.setattr(Settings, "withdrawal_min_interval_seconds", 0)
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(93003)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("10"), "DEPOSIT", "deposit", 1)
    req = wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")
    idem = conn.execute("SELECT idempotency_key FROM withdrawals WHERE id=?", (req["id"],)).fetchone()["idempotency_key"]
    conn.execute("UPDATE withdrawals SET status='submitted', provider_origin='external_http', provider_ref='r1', idempotency_key=?, submitted_at=datetime('now','-120 seconds') WHERE id=?", (idem, req["id"]))

    class Provider:
        provider_origin = "external_http"
        def is_ready(self): return True, None
        def execute_withdrawal(self, _req): raise RuntimeError("no")
        def reconcile_withdrawal(self, _req):
            from signer.withdrawal_provider import WithdrawalReconciliationResult
            return WithdrawalReconciliationResult(status="submitted", provider_ref="r2", asset="USDT")

    svc = SignerService(); svc.provider = Provider(); svc.process_withdrawals(wallet)
    row = conn.execute("SELECT status,failure_reason FROM withdrawals WHERE id=?", (req["id"],)).fetchone()
    assert row["status"] == "signer_retry"
    assert "provider_ref mismatch" in str(row["failure_reason"] or "")


def test_signer_execute_asset_mismatch_and_malformed_txid_fail_closed(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", True)
    monkeypatch.setattr(Settings, "withdrawal_min_interval_seconds", 0)
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(93004)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("10"), "DEPOSIT", "deposit", 1)
    req1 = wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")

    class Provider1:
        provider_origin = "external_http"
        def is_ready(self): return True, None
        def execute_withdrawal(self, _req):
            from signer.withdrawal_provider import WithdrawalExecutionResult
            return WithdrawalExecutionResult(status="submitted", provider_ref="r1", asset="ETH")
        def reconcile_withdrawal(self, _req): raise RuntimeError("no")

    svc = SignerService(); svc.provider = Provider1(); svc.process_withdrawals(wallet)
    assert conn.execute("SELECT status FROM withdrawals WHERE id=?", (req1["id"],)).fetchone()["status"] == "signer_retry"

    req2 = wallet.request_withdrawal(uid, "USDT", Decimal("1"), "0x1111111111111111111111111111111111111111")

    class Provider2:
        provider_origin = "external_http"
        def is_ready(self): return True, None
        def execute_withdrawal(self, _req):
            from signer.withdrawal_provider import WithdrawalExecutionResult
            return WithdrawalExecutionResult(status="broadcasted", provider_ref="r2", txid="bad", asset="USDT")
        def reconcile_withdrawal(self, _req): raise RuntimeError("no")

    svc.provider = Provider2(); svc.process_withdrawals(wallet)
    assert conn.execute("SELECT status FROM withdrawals WHERE id=?", (req2["id"],)).fetchone()["status"] == "signer_retry"


def test_reconcile_backoff_uses_last_reconciled_at(conn):
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(93005)
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key,submitted_at) VALUES(?,?,?,?,?,?,datetime('now'))", (uid, "USDT", "1", "a", "submitted", "k-sub-now"))
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key,submitted_at) VALUES(?,?,?,?,?,?,datetime('now','-600 seconds'))", (uid, "USDT", "1", "b", "submitted", "k-sub-old"))
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key,broadcasted_at) VALUES(?,?,?,?,?,?,datetime('now'))", (uid, "USDT", "1", "c", "broadcasted", "k-br-now"))
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key,broadcasted_at) VALUES(?,?,?,?,?,?,datetime('now','-600 seconds'))", (uid, "USDT", "1", "d", "broadcasted", "k-br-old"))
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key,last_reconciled_at) VALUES(?,?,?,?,?,?,datetime('now'))", (uid, "USDT", "1", "e", "signer_retry", "k-retry-now"))
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key,last_reconciled_at) VALUES(?,?,?,?,?,?,datetime('now','-600 seconds'))", (uid, "USDT", "1", "f", "signer_retry", "k-retry-old"))
    rows = wallet.unresolved_withdrawals_for_reconcile(limit=20, submitted_after_s=45, broadcasted_after_s=120, signer_retry_after_s=300)
    keys = {str(r["idempotency_key"]) for r in rows}
    assert "k-sub-old" in keys and "k-sub-now" not in keys
    assert "k-br-old" in keys and "k-br-now" not in keys
    assert "k-retry-old" in keys and "k-retry-now" not in keys


def test_error_sanitizer_redacts_secrets_and_payloads():
    raw = "Bearer abc123 token=xyz authorization:secret https://user:pass@example.com Traceback (most recent call last): boom {\"k\":\"v\"}"
    safe = sanitize_runtime_error(raw)
    assert "abc123" not in safe
    assert "user:pass" not in safe
    assert "Traceback" not in safe


def test_run_bot_fatal_startup_error_classification():
    status = PreflightStatus(service_name="bot", reasons=("route integrity failed",), route_integrity_ready=False)
    assert run_bot._is_fatal_startup_error(PreflightIntegrityError(status)) is True
    assert run_bot._is_fatal_startup_error(RuntimeError("TELEGRAM_BOT_TOKEN is required")) is True
    assert run_bot._is_fatal_startup_error(RuntimeError("worker crashed after startup")) is False


def test_record_withdrawal_provider_result_rejects_oversized_or_malformed_metadata(conn):
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(98001)
    conn.execute("INSERT INTO withdrawals(user_id,asset,amount,destination_address,status,idempotency_key) VALUES(?,?,?,?,?,?)", (uid, "USDT", "1", "enc", "pending", "k-meta"))
    wid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    class BadType:
        status = "submitted"
        provider_ref = "r-meta"
        asset = "USDT"
        external_status = "submitted"
        submitted_at = None
        broadcasted_at = None
        metadata = ["not", "dict"]

    with pytest.raises(RuntimeError, match="metadata must be an object"):
        wallet.record_withdrawal_provider_result(wid, "external_http", "k-meta", BadType())

    class TooLarge:
        status = "submitted"
        provider_ref = "r-meta"
        asset = "USDT"
        external_status = "submitted"
        submitted_at = None
        broadcasted_at = None
        metadata = {"blob": "x" * 5000}

    with pytest.raises(RuntimeError, match="metadata too large"):
        wallet.record_withdrawal_provider_result(wid, "external_http", "k-meta", TooLarge())


def test_withdrawal_lifecycle_execute_and_reconcile_to_confirmed(conn, monkeypatch):
    monkeypatch.setattr(Settings, "withdrawals_enabled", True)
    monkeypatch.setattr(Settings, "withdrawal_min_interval_seconds", 0)
    wallet = WalletService(conn)
    uid = wallet._ensure_user_row(98002)
    wallet.ledger.add_entry("USER", uid, uid, "USDT", Decimal("20"), "DEPOSIT", "deposit", 101)
    req = wallet.request_withdrawal(uid, "USDT", Decimal("2"), "0x1111111111111111111111111111111111111111")

    class Provider:
        provider_origin = "external_http"
        def is_ready(self):
            return True, None
        def execute_withdrawal(self, _req):
            from signer.withdrawal_provider import WithdrawalExecutionResult
            return WithdrawalExecutionResult(status="submitted", provider_ref="r-lc", asset="USDT", external_status="queued")
        def reconcile_withdrawal(self, _req):
            from signer.withdrawal_provider import WithdrawalReconciliationResult
            return WithdrawalReconciliationResult(status="confirmed", provider_ref="r-lc", txid="0x" + "a"*64, asset="USDT", external_status="confirmed")

    svc = SignerService()
    svc.provider = Provider()
    svc.process_withdrawals(wallet)
    conn.execute("UPDATE withdrawals SET submitted_at=datetime('now','-600 seconds') WHERE id=?", (req["id"],))
    svc.process_withdrawals(wallet)
    row = conn.execute("SELECT status,provider_ref,txid FROM withdrawals WHERE id=?", (req["id"],)).fetchone()
    assert row["status"] == "confirmed"
    assert row["provider_ref"] == "r-lc"
    assert str(row["txid"]).startswith("0x")
