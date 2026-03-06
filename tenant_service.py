from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fee_service import FeeService


@dataclass
class TenantView:
    bot_id: int
    owner_user_id: int
    bot_display_name: str
    support_contact: str | None
    bot_extra_fee_percent: Decimal


class TenantService:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.fee_service = FeeService()

    def ensure_user(self, telegram_id: int, username: str | None = None) -> int:
        row = self.conn.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute("INSERT INTO users(telegram_id,username) VALUES(?,?)", (telegram_id, username))
        return int(cur.lastrowid)

    def create_or_update_tenant(self, bot_id: int, owner_user_id: int, bot_display_name: str, support_contact: str, bot_extra_fee_percent: Decimal) -> TenantView:
        pct = self.fee_service.validate_bot_extra_fee_percent(bot_extra_fee_percent)
        exists = self.conn.execute("SELECT id FROM bots WHERE id=?", (bot_id,)).fetchone()
        if exists:
            self.conn.execute(
                "UPDATE bots SET owner_user_id=?, display_name=?, support_contact=?, bot_extra_fee_percent=? WHERE id=?",
                (owner_user_id, bot_display_name, support_contact, str(pct), bot_id),
            )
        else:
            self.conn.execute(
                "INSERT INTO bots(id, owner_user_id, display_name, support_contact, bot_extra_fee_percent) VALUES(?,?,?,?,?)",
                (bot_id, owner_user_id, bot_display_name, support_contact, str(pct)),
            )
        return TenantView(bot_id, owner_user_id, bot_display_name, support_contact, pct)

    def get_tenant(self, bot_id: int) -> TenantView | None:
        row = self.conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
        if not row:
            return None
        return TenantView(int(row["id"]), int(row["owner_user_id"]), row["display_name"], row["support_contact"], Decimal(row["bot_extra_fee_percent"]))
