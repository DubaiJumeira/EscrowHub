from __future__ import annotations

import json
import os

from config.settings import Settings
from decimal import Decimal
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit
from watcher_status_service import read_watcher_cursor

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class EthRpcAdapter(ChainAdapter):
    def __init__(self, address_user_map: dict[str, int], conn=None) -> None:
        self.rpc_url = os.getenv("ETH_RPC_URL", "")
        self.conn = conn
        self.address_user_map = {k.lower(): int(v) for k, v in address_user_map.items()}
        self.confirmations_required = int(os.getenv("ETH_CONFIRMATIONS_REQUIRED", "12"))
        self.usdt_contract = os.getenv("USDT_ERC20_CONTRACT", "").lower()
        self.network = os.getenv("ETH_NETWORK", "ethereum")
        self.app_env = os.getenv("APP_ENV", "dev").lower()
        self.max_blocks_per_run = max(1, int(Settings.eth_max_blocks_per_run))

    def _rpc(self, method: str, params: list) -> dict:
        if not self.rpc_url:
            return {}
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
        req = Request(self.rpc_url, method="POST", headers={"Content-Type": "application/json"}, data=payload)
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def _hex_to_int(value: str | None) -> int:
        return int(value, 16) if value else 0

    @staticmethod
    def _normalize_hex_address(raw: str | None) -> str:
        if not raw:
            return ""
        h = raw.lower().replace("0x", "")
        return "0x" + h[-40:] if len(h) >= 40 else ""

    def _finalized_head(self) -> int:
        head = self._rpc("eth_blockNumber", []).get("result")
        return max(0, self._hex_to_int(head) - self.confirmations_required)

    def _load_cursor(self) -> int:
        env_floor = int(os.getenv("ETH_START_BLOCK", "0"))
        if self.conn is None:
            return env_floor
        current = read_watcher_cursor(self.conn, "eth_watcher")
        if current is None:
            return env_floor
        return max(env_floor, int(current))

    def _event_amount(self, units_hex: str, asset: str) -> Decimal:
        units = Decimal(self._hex_to_int(units_hex))
        return units / (Decimal("10") ** (Decimal("18") if asset == "ETH" else Decimal("6")))

    def _fetch_env_events(self) -> list[dict]:
        raw_events = os.getenv("ETH_DEPOSIT_EVENTS_JSON", "[]")
        if raw_events.strip() not in {"", "[]"} and self.app_env != "test":
            # WARNING: Environment-fed deposit events are insecure outside tests.
            raise RuntimeError("ETH_DEPOSIT_EVENTS_JSON ingestion is allowed only in APP_ENV=test")
        return json.loads(raw_events) if raw_events.strip() else []

    def fetch_deposits(self) -> tuple[list[ChainDeposit], int | None]:
        if self.app_env == "test":
            deposits: list[ChainDeposit] = []
            for ev in self._fetch_env_events():
                to_addr = self._normalize_hex_address(ev.get("to"))
                uid = self.address_user_map.get(to_addr.lower())
                if not uid:
                    continue
                asset = (ev.get("asset") or "ETH").upper()
                amount = Decimal(str(ev.get("amount") or "0"))
                if amount <= 0:
                    continue
                txid = str(ev.get("txid") or "").lower()
                if not txid:
                    continue
                deposits.append(ChainDeposit(uid, asset, amount, txid, ev.get("unique_key") or f"test:{txid}:{asset}:{to_addr}", int(ev.get("confirmations", self.confirmations_required)), True))
            return deposits, None

        if not self.rpc_url:
            raise RuntimeError("ETH_RPC_URL is required for production ETH/USDT polling")
        finalized = self._finalized_head()
        start = self._load_cursor()
        if finalized <= start:
            return [], finalized
        end_block = min(finalized, start + self.max_blocks_per_run)

        watched_topics = ["0x" + "0" * 24 + a.lower().replace("0x", "") for a in self.address_user_map.keys()]
        deposits: list[ChainDeposit] = []

        for block_num in range(start + 1, end_block + 1):
            block = self._rpc("eth_getBlockByNumber", [hex(block_num), True]).get("result") or {}
            for tx in block.get("transactions", []) or []:
                to_addr = self._normalize_hex_address(tx.get("to"))
                uid = self.address_user_map.get(to_addr.lower())
                if not uid:
                    continue
                value_hex = tx.get("value", "0x0")
                amount = self._event_amount(value_hex, "ETH")
                txid = str(tx.get("hash") or "").lower()
                if txid and amount > 0:
                    deposits.append(ChainDeposit(uid, "ETH", amount, txid, f"{self.network}:eth:{txid}:ETH:0:{to_addr}", self.confirmations_required, True))

        if self.usdt_contract:
            erc20_logs = self._rpc("eth_getLogs", [{"fromBlock": hex(start + 1), "toBlock": hex(end_block), "address": self.usdt_contract, "topics": [TRANSFER_TOPIC, None, watched_topics]}]).get("result", [])
            for ev in erc20_logs:
                topics = ev.get("topics") or []
                if len(topics) < 3:
                    continue
                to_addr = self._normalize_hex_address(topics[2])
                uid = self.address_user_map.get(to_addr.lower())
                if not uid:
                    continue
                txid = str(ev.get("transactionHash") or "").lower()
                amount = self._event_amount(ev.get("data", "0x0"), "USDT")
                if txid and amount > 0:
                    li = self._hex_to_int(ev.get("logIndex"))
                    deposits.append(ChainDeposit(uid, "USDT", amount, txid, f"{self.network}:eth:{txid}:USDT:{li}:{to_addr}", self.confirmations_required, True))
        return deposits, end_block

    def broadcast_raw_transaction(self, asset: str, raw_tx_hex: str) -> str:
        return self._rpc("eth_sendRawTransaction", [raw_tx_hex]).get("result", "")
