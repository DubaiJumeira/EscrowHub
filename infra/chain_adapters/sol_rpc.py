from __future__ import annotations

import json
import os
from decimal import Decimal
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit
from watcher_status_service import read_watcher_cursor

LAMPORTS_PER_SOL = Decimal("1000000000")


class SolRpcAdapter(ChainAdapter):
    def __init__(self, address_user_map: dict[str, int], conn=None) -> None:
        self.rpc_url = os.getenv("SOL_RPC_URL", "").strip()
        self.conn = conn
        self.address_user_map = {
            str(address).strip(): int(user_id)
            for address, user_id in address_user_map.items()
            if str(address).strip()
        }
        self.confirmations_required = int(os.getenv("SOL_CONFIRMATIONS_REQUIRED", "32"))
        self.network = os.getenv("SOL_NETWORK", "solana").strip() or "solana"
        self.app_env = os.getenv("APP_ENV", "dev").lower()
        self.max_slots_per_run = max(1, int(os.getenv("SOL_MAX_SLOTS_PER_RUN", "64")))

    def _rpc(self, method: str, params: list) -> dict:
        if not self.rpc_url:
            return {}
        payload = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        ).encode()
        req = Request(
            self.rpc_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=payload,
        )
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def _finalized_head(self) -> int:
        head = self._rpc("getSlot", [{"commitment": "finalized"}]).get("result")
        return int(head or 0)

    def _load_cursor(self) -> int:
        env_floor = int(os.getenv("SOL_START_SLOT", "0"))
        if self.conn is None:
            return env_floor
        current = read_watcher_cursor(self.conn, "sol_watcher")
        if current is None:
            return env_floor
        return max(env_floor, int(current))

    @staticmethod
    def _amount_from_lamports(lamports: int | str | None) -> Decimal:
        raw = int(lamports or 0)
        return Decimal(raw) / LAMPORTS_PER_SOL

    def _fetch_env_events(self) -> list[dict]:
        raw_events = os.getenv("SOL_DEPOSIT_EVENTS_JSON", "[]")
        if raw_events.strip() not in {"", "[]"} and self.app_env != "test":
            raise RuntimeError("SOL_DEPOSIT_EVENTS_JSON ingestion is allowed only in APP_ENV=test")
        return json.loads(raw_events) if raw_events.strip() else []

    @staticmethod
    def _iter_transfer_instructions(tx: dict) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        transaction = tx.get("transaction") or {}
        message = transaction.get("message") or {}
        for idx, inst in enumerate(message.get("instructions") or []):
            out.append((f"outer:{idx}", inst))

        meta = tx.get("meta") or {}
        for outer in meta.get("innerInstructions") or []:
            outer_index = int(outer.get("index") or 0)
            for inner_idx, inst in enumerate(outer.get("instructions") or []):
                out.append((f"inner:{outer_index}:{inner_idx}", inst))
        return out

    def _append_matching_instruction(
        self,
        deposits: list[ChainDeposit],
        txid: str,
        instruction_key: str,
        inst: dict,
    ) -> None:
        if str(inst.get("program") or "").strip() != "system":
            return
        parsed = inst.get("parsed")
        if not isinstance(parsed, dict):
            return
        if str(parsed.get("type") or "").strip() != "transfer":
            return
        info = parsed.get("info") or {}
        to_addr = str(info.get("destination") or "").strip()
        uid = self.address_user_map.get(to_addr)
        if not uid:
            return
        lamports = int(info.get("lamports") or 0)
        if lamports <= 0:
            return
        amount = self._amount_from_lamports(lamports)
        deposits.append(
            ChainDeposit(
                uid,
                "SOL",
                amount,
                txid,
                f"{self.network}:sol:{txid}:SOL:{instruction_key}:{to_addr}",
                self.confirmations_required,
                True,
            )
        )

    def fetch_deposits(self) -> tuple[list[ChainDeposit], int | None]:
        if self.app_env == "test":
            deposits: list[ChainDeposit] = []
            for ev in self._fetch_env_events():
                to_addr = str(ev.get("to") or "").strip()
                uid = self.address_user_map.get(to_addr)
                if not uid:
                    continue
                asset = str(ev.get("asset") or "SOL").upper().strip()
                amount = Decimal(str(ev.get("amount") or "0"))
                txid = str(ev.get("txid") or "").strip()
                if asset != "SOL" or amount <= 0 or not txid:
                    continue
                deposits.append(
                    ChainDeposit(
                        uid,
                        "SOL",
                        amount,
                        txid,
                        ev.get("unique_key") or f"test:{txid}:SOL:{to_addr}",
                        int(ev.get("confirmations", self.confirmations_required)),
                        True,
                    )
                )
            return deposits, None

        if not self.rpc_url:
            raise RuntimeError("SOL_RPC_URL is required for production SOL polling")

        finalized = self._finalized_head()
        start = self._load_cursor()
        if finalized <= start:
            return [], finalized

        end_slot = min(finalized, start + self.max_slots_per_run)
        deposits: list[ChainDeposit] = []

        for slot in range(start + 1, end_slot + 1):
            block = self._rpc(
                "getBlock",
                [
                    slot,
                    {
                        "encoding": "jsonParsed",
                        "transactionDetails": "full",
                        "rewards": False,
                        "maxSupportedTransactionVersion": 0,
                        "commitment": "finalized",
                    },
                ],
            ).get("result")
            if not block:
                continue

            for tx in block.get("transactions") or []:
                transaction = tx.get("transaction") or {}
                signatures = transaction.get("signatures") or []
                txid = str(signatures[0] or "").strip() if signatures else ""
                if not txid:
                    continue

                for instruction_key, inst in self._iter_transfer_instructions(tx):
                    self._append_matching_instruction(deposits, txid, instruction_key, inst)

        return deposits, end_slot

    def broadcast_raw_transaction(self, asset: str, raw_tx_hex: str) -> str:
        return self._rpc(
            "sendTransaction",
            [raw_tx_hex, {"encoding": "base64", "preflightCommitment": "confirmed"}],
        ).get("result", "")
