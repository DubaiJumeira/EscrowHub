from decimal import Decimal

import pytest

from config.settings import Settings
from escrow_service import EscrowService
from hd_wallet import HDWalletDeriver
from signer.signer_service import DisabledSignerProvider, SignerService
from tenant_service import TenantService
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
