from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class User:
    id: int
    telegram_id: int
    username: str | None
    is_frozen: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class BotTenant:
    id: int
    owner_user_id: int
    display_name: str
    service_fee_percent: Decimal
    bot_token_hash: str
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Escrow:
    id: int
    tenant_bot_id: int
    asset: str
    amount: Decimal
    buyer_user_id: int
    seller_user_id: int
    status: str
    description: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class LedgerEntry:
    id: int
    user_id: int
    asset: str
    amount: Decimal
    direction: str
    entry_type: str
    reference_type: str
    reference_id: int
    idempotency_key: str
    created_at: datetime = field(default_factory=datetime.utcnow)
