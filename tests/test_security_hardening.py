from decimal import Decimal
import json

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
    tenant.create_or_update_tenant(1, owner, "bot", "@support", Decimal("2"))
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
    tenant.create_or_update_tenant(77, owner, "mybot", "@support", Decimal("1"))
    router = TenantRouter(conn)
    ctx = router.resolve_tenant("@mybot")
    assert ctx.tenant_bot_id == 77
    with pytest.raises(TenantNotFoundError):
        router.resolve_tenant("@missing")
