from decimal import Decimal
import json
import os

import pytest

from apps.bot_tenant_router.router import TenantNotFoundError, TenantRouter
from config.settings import Settings
from escrow_service import EscrowService
from hd_wallet import HDWalletDeriver
from infra.chain_adapters.eth_rpc import EthRpcAdapter
from signer.signer_service import DisabledSignerProvider, SignerService
from tenant_service import TenantService
from wallet_service import WalletService
from watcher_status_service import read_watcher_cursor, write_watcher_cursor
import bot
import run_signer
import run_btc_watcher
import run_eth_watcher
from runtime_preflight import PreflightStatus, run_startup_preflight
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
    with pytest.raises(RuntimeError):
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
    enc_addr = wallet._encrypt_field("bc1qtestxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    conn.execute(
        "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)",
        (uid, "BTC", "BTC", enc_addr, uid, None, None),
    )
    result = wallet.monitored_deposit_address_map(["BTC"])
    assert result == {"bc1qtestxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx": uid}


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
