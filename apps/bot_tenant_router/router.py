from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


class TenantNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class TenantContext:
    tenant_bot_id: int
    owner_user_id: int
    service_fee_percent: str


class TenantRouter:
    def __init__(self, conn, ttl_seconds: int = 30) -> None:
        self.conn = conn
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._cache: dict[str, tuple[datetime, TenantContext]] = {}

    def resolve_tenant(self, bot_username: str) -> TenantContext:
        username = (bot_username or "").strip().lstrip("@").lower()
        if not username:
            raise TenantNotFoundError("unknown tenant")
        cached = self._cache.get(username)
        now = datetime.utcnow()
        if cached and (now - cached[0]) <= timedelta(seconds=self.ttl_seconds):
            return cached[1]
        row = self.conn.execute(
            "SELECT id, owner_user_id, bot_extra_fee_percent FROM bots WHERE lower(display_name)=?",
            (username,),
        ).fetchone()
        if not row:
            raise TenantNotFoundError("unknown tenant")
        ctx = TenantContext(int(row["id"]), int(row["owner_user_id"]), str(row["bot_extra_fee_percent"]))
        self._cache[username] = (now, ctx)
        return ctx
