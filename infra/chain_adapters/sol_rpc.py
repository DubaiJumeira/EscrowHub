from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit


class SolRpcAdapter(ChainAdapter):
    def __init__(self, address_user_map: dict[str, int]) -> None:
        self.rpc_url = os.getenv("SOL_RPC_URL", "")
        self.address_user_map = address_user_map

    def _rpc(self, method: str, params: list[Any], retries: int = 3) -> Any:
        if not self.rpc_url:
            raise RuntimeError("SOL_RPC_URL not configured")
        req = Request(
            self.rpc_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        )
        delay = 0.5
        for i in range(retries):
            try:
                with urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode())
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return data["result"]
            except Exception:
                if i == retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2

    def get_latest_slot(self) -> int:
        return int(self._rpc("getSlot", [{"commitment": "finalized"}]))

    def get_signatures_for_address(self, address: str, before: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        cfg: dict[str, Any] = {"limit": limit}
        if before:
            cfg["before"] = before
        return list(self._rpc("getSignaturesForAddress", [address, cfg]))

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return self._rpc("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])

    def fetch_deposits_since_signature(self, last_signature: str | None = None) -> tuple[list[ChainDeposit], str | None]:
        if not self.rpc_url:
            return [], last_signature
        out: list[ChainDeposit] = []
        newest_seen = last_signature
        for address, user_id in self.address_user_map.items():
            sigs = self.get_signatures_for_address(address, before=None, limit=50)
            for idx, sig_row in enumerate(sigs):
                sig = sig_row["signature"]
                if idx == 0:
                    newest_seen = sig
                if last_signature and sig == last_signature:
                    break
                if sig_row.get("confirmationStatus") != "finalized":
                    continue
                tx = self.get_transaction(sig)
                if not tx:
                    continue
                instructions = tx.get("transaction", {}).get("message", {}).get("instructions", [])
                for i, inst in enumerate(instructions):
                    parsed = inst.get("parsed", {})
                    if parsed.get("type") != "transfer":
                        continue
                    info = parsed.get("info", {})
                    if info.get("destination") != address:
                        continue
                    lamports = Decimal(str(info.get("lamports", "0")))
                    amount = lamports / Decimal("1000000000")
                    out.append(ChainDeposit(user_id, "SOL", amount, sig, f"{sig}:{i}", 1, True))
        return out, newest_seen

    def fetch_deposits(self) -> list[ChainDeposit]:
        deps, _ = self.fetch_deposits_since_signature(None)
        return deps
