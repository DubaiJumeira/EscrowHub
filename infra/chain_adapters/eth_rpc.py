from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from typing import Any
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit

LOGGER = logging.getLogger(__name__)
TRANSFER_TOPIC = "0xddf252ad" + "0" * 56


class EthRpcAdapter(ChainAdapter):
    def __init__(self, address_user_map: dict[str, int]) -> None:
        self.rpc_url = os.getenv("ETH_RPC_URL", "")
        self.address_user_map = {k.lower(): v for k, v in address_user_map.items()}
        self.min_conf = int(os.getenv("ETH_CONFIRMATIONS", "12"))
        self.usdt_contract = os.getenv("USDT_ERC20_CONTRACT", "").lower()
        self.usdc_contract = os.getenv("USDC_ERC20_CONTRACT", "").lower()

    def _rpc(self, method: str, params: list[Any], retries: int = 3) -> Any:
        if not self.rpc_url:
            raise RuntimeError("ETH_RPC_URL is not configured")
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
        req = Request(self.rpc_url, method="POST", headers={"Content-Type": "application/json"}, data=body)
        delay = 0.5
        for attempt in range(retries):
            try:
                with urlopen(req, timeout=20) as resp:
                    payload = json.loads(resp.read().decode())
                if "error" in payload:
                    raise RuntimeError(f"eth rpc error: {payload['error']}")
                return payload["result"]
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2

    @staticmethod
    def _hex_to_int(v: str) -> int:
        return int(v, 16)

    def get_latest_block(self) -> int:
        return self._hex_to_int(self._rpc("eth_blockNumber", []))

    def get_block_transactions(self, block_number: int) -> list[dict[str, Any]]:
        block = self._rpc("eth_getBlockByNumber", [hex(block_number), True])
        if not block:
            return []
        return block.get("transactions", [])

    def get_native_transfers_to(self, addresses: set[str], from_block: int, to_block: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for block_number in range(from_block, to_block + 1):
            for tx in self.get_block_transactions(block_number):
                to_addr = (tx.get("to") or "").lower()
                if to_addr in addresses and tx.get("value") not in (None, "0x0"):
                    rows.append({"txhash": tx["hash"], "to": to_addr, "value": tx["value"], "block": block_number})
        return rows

    def get_erc20_transfers_to(self, addresses: set[str], token_contract: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
        if not token_contract:
            return []
        topics = [TRANSFER_TOPIC]
        logs = self._rpc(
            "eth_getLogs",
            [{"fromBlock": hex(from_block), "toBlock": hex(to_block), "address": token_contract, "topics": topics}],
        )
        rows: list[dict[str, Any]] = []
        for lg in logs:
            if len(lg.get("topics", [])) < 3:
                continue
            to_addr = "0x" + lg["topics"][2][-40:]
            if to_addr.lower() not in addresses:
                continue
            rows.append(
                {
                    "txhash": lg["transactionHash"],
                    "log_index": self._hex_to_int(lg["logIndex"]),
                    "to": to_addr.lower(),
                    "value": lg["data"],
                    "block": self._hex_to_int(lg["blockNumber"]),
                }
            )
        return rows

    def fetch_deposits_between(self, from_block: int, to_block: int) -> list[ChainDeposit]:
        if not self.rpc_url:
            return []
        addresses = set(self.address_user_map.keys())
        latest = self.get_latest_block()
        out: list[ChainDeposit] = []

        for tx in self.get_native_transfers_to(addresses, from_block, to_block):
            conf = max(0, latest - tx["block"])
            amount = Decimal(int(tx["value"], 16)) / Decimal("1000000000000000000")
            user_id = self.address_user_map[tx["to"]]
            key = f"{tx['txhash']}:{tx['to']}:{tx['value']}"
            out.append(ChainDeposit(user_id, "ETH", amount, tx["txhash"], key, conf, conf >= self.min_conf))

        token_defs = [("USDT", self.usdt_contract, Decimal("1000000")), ("USDC", self.usdc_contract, Decimal("1000000"))]
        for symbol, contract, decimals in token_defs:
            for lg in self.get_erc20_transfers_to(addresses, contract, from_block, to_block):
                conf = max(0, latest - lg["block"])
                amount = Decimal(int(lg["value"], 16)) / decimals
                user_id = self.address_user_map[lg["to"]]
                out.append(ChainDeposit(user_id, symbol, amount, lg["txhash"], f"{lg['txhash']}:{lg['log_index']}", conf, conf >= self.min_conf))

        LOGGER.info("ETH adapter scanned %s-%s found=%s", from_block, to_block, len(out))
        return out

    def fetch_deposits(self) -> list[ChainDeposit]:
        latest = self.get_latest_block() if self.rpc_url else 0
        return self.fetch_deposits_between(latest, latest) if latest else []
