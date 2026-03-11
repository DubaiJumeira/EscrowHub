from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from config.settings import Settings
from hd_wallet import HDWalletDeriver
from infra.db.database import init_db
from wallet_service import WalletService


class _FakeCtx:
    def __init__(self, path_parts=None):
        self.parts = path_parts or []
    def Purpose(self):
        return self
    def Coin(self):
        return self
    def Account(self, v):
        self.parts.append(("account", v))
        return self
    def Change(self, v):
        self.parts.append(("change", v))
        return self
    def AddressIndex(self, v):
        self.parts.append(("index", v))
        return self
    def PublicKey(self):
        return self
    def ToAddress(self):
        acct = next(v for k, v in self.parts if k == "account")
        idx = next(v for k, v in self.parts if k == "index")
        return f"addr-{acct}-{idx}"


class _FakeBip84:
    @staticmethod
    def FromSeed(seed, coin):
        return _FakeCtx([])
    @staticmethod
    def FromExtendedKey(xpub, coin):
        return _FakeCtx([])


class _FakeBip44(_FakeBip84):
    pass


class _FakeB:
    Bip84 = _FakeBip84
    Bip44 = _FakeBip44
    Bip84Coins = SimpleNamespace(BITCOIN="btc", LITECOIN="ltc")
    Bip44Coins = SimpleNamespace(ETHEREUM="eth")
    Bip44Changes = SimpleNamespace(CHAIN_EXT=0)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    yield c
    c.close()


def test_production_local_hd_derivation_allowed(monkeypatch):
    monkeypatch.setattr(Settings, "is_production", True)
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setenv("ADDRESS_PROVIDER", "local_hd")
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ab" * 32)
    d = HDWalletDeriver()
    monkeypatch.setattr(d, "_bip_utils", lambda: _FakeB)
    out = d.derive_btc_address(1)
    assert out.public_address == "addr-0-1"
    assert out.path == "m/84'/0'/0'/0/1"


def test_large_user_id_maps_below_hardened_limit(monkeypatch):
    monkeypatch.setattr(Settings, "is_production", False)
    monkeypatch.setattr(Settings, "btc_xpub", "")
    monkeypatch.setenv("HD_WALLET_SEED_HEX", "ab" * 32)
    d = HDWalletDeriver()
    monkeypatch.setattr(d, "_bip_utils", lambda: _FakeB)
    out = d.derive_btc_address(8315030455)
    assert out.public_address == "addr-3-1872579511"
    assert out.path == "m/84'/0'/3'/0/1872579511"


def test_invalid_btc_and_ltc_addresses_rejected(conn):
    wallet = WalletService(conn)
    with pytest.raises(ValueError):
        wallet.validate_withdrawal_address("BTC", "bc1invalid")
    with pytest.raises(ValueError):
        wallet.validate_withdrawal_address("BTC", "1111111111111111111114oLvT3")
    with pytest.raises(ValueError):
        wallet.validate_withdrawal_address("LTC", "ltc1invalid")


def test_valid_btc_and_ltc_addresses_accepted(conn):
    wallet = WalletService(conn)
    wallet.validate_withdrawal_address("BTC", "bc1qjk93nux8jyflhhajqqcw2gha9ak06fjrqpznmx")
    wallet.validate_withdrawal_address("BTC", "1BoatSLRHtKNngkdXEeobR76b53LETtpyT")
    wallet.validate_withdrawal_address("LTC", "LLnCCHbSzfwWquEdaS5TF2Yt7uz5Qb1SZ1")
