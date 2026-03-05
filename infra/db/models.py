from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class BotTenant:
    id: int
    owner_user_id: int
    bot_display_name: str
    support_contact: str
    bot_extra_fee_percent: Decimal
    bot_token_hash: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Escrow:
    id: int
    bot_id: int
    buyer_id: int
    seller_id: int
    asset: str
    amount: Decimal
    platform_fee: Decimal
    bot_extra_fee: Decimal
    total_fee: Decimal
    seller_payout: Decimal
    status: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class WalletAddress:
    id: int
    user_id: int
    asset: str
    address: str
    xrp_destination_tag: str | None = None


@dataclass
class Deposit:
    id: int
    user_id: int
    asset: str
    amount: Decimal
    tx_hash: str
    confirmations: int


@dataclass
class Withdrawal:
    id: int
    user_id: int
    asset: str
    amount: Decimal
    to_address: str
    status: str
    tx_hash: str | None = None


@dataclass
class LedgerEntry:
    id: int
    user_id: int | None
    asset: str
    amount: Decimal
    entry_type: str
    direction: str
    reference_type: str
    reference_id: int
    created_at: datetime = field(default_factory=datetime.utcnow)
