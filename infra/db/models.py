from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class LedgerEntry:
    id: int
    user_id: int | None
    account_type: str
    account_owner_id: int | None
    asset: str
    amount: Decimal
    entry_type: str
    ref_type: str
    ref_id: int
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class EscrowLock:
    escrow_id: int
    user_id: int
    asset: str
    amount: Decimal
    status: str


@dataclass
class WalletAddress:
    id: int
    user_id: int
    asset: str
    chain_family: str
    address: str
    derivation_index: int | None


@dataclass
class XrpRoute:
    user_id: int
    xrp_receive_address: str
    destination_tag: str


@dataclass
class Deposit:
    id: int
    user_id: int
    asset: str
    amount: Decimal
    txid: str
    chain_family: str
    unique_key: str
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
    txid: str | None = None
