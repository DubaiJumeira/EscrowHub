from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class User:
    id: int
    telegram_id: int
    username: str | None
    frozen: int


@dataclass
class TenantBot:
    id: int
    owner_user_id: int
    bot_extra_fee_percent: Decimal
    support_contact: str | None
    display_name: str


@dataclass
class WalletAddress:
    id: int
    user_id: int
    asset: str
    chain_family: str
    address: str
    derivation_index: int | None
    derivation_path: str | None
    destination_tag: str | None


@dataclass
class Deposit:
    id: int
    user_id: int
    asset: str
    amount: Decimal
    txid: str
    unique_key: str
    chain_family: str
    confirmations: int
    status: str


@dataclass
class Withdrawal:
    id: int
    user_id: int | None
    asset: str
    amount: Decimal
    destination_address: str
    status: str
    txid: str | None


@dataclass
class ChainScanState:
    chain_family: str
    cursor: str


@dataclass
class Sweep:
    id: int
    asset: str
    amount: Decimal
    to_cold_address: str
    txid: str | None
    status: str
