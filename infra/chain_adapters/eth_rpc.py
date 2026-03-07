from __future__ import annotations

import json
import os
from decimal import Decimal
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit

TRANSFER_TOPIC = "0xddf252ad00000000000000000000000000000000000000000000000000000000"


class EthRpcAdapter(ChainAdapter):
    def __init__(self, address_user_map: dict[str, int]) -> None:
        self.rpc_url = os.getenv("ETH_RPC_URL", "")
        self.address_user_map = {k.lower(): int(v) for k, v in address_user_map.items()}
        self.confirmations_required = int(os.getenv("ETH_CONFIRMATIONS_REQUIRED", "12"))
        self.usdt_contract = os.getenv("USDT_ERC20_CONTRACT", "").lower()
        self.usdc_contract = os.getenv("USDC_ERC20_CONTRACT", "").lower()

    def _rpc(self, method: str, params: list) -> dict:
        if not self.rpc_url:
            return {}
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
        req = Request(self.rpc_url, method="POST", headers={"Content-Type": "application/json"}, data=payload)
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def _hex_to_int(value: str | None) -> int:
        if not value:
            return 0
        return int(value, 16)

    @staticmethod
    def _normalize_hex_address(raw: str | None) -> str:
        if not raw:
            return ""
        h = raw.lower().replace("0x", "")
        if len(h) < 40:
            return ""
        return "0x" + h[-40:]

    def _asset_for_contract(self, contract_address: str | None) -> str | None:
        contract = (contract_address or "").lower()
        if not contract:
            return "ETH"
        if self.usdt_contract and contract == self.usdt_contract:
            return "USDT"
        if self.usdc_contract and contract == self.usdc_contract:
            return "USDC"
        return None

    def _event_amount(self, value_hex: str | None, asset: str) -> Decimal:
        units = Decimal(self._hex_to_int(value_hex))
        decimals = Decimal("18") if asset == "ETH" else Decimal("6")
        return units / (Decimal("10") ** decimals)

    def _parse_event(self, event: dict) -> ChainDeposit | None:
        to_addr = ""
        asset = "ETH"
        if event.get("type") == "erc20":
            asset = self._asset_for_contract(event.get("contract_address")) or ""
            if not asset:
                return None
            to_addr = self._normalize_hex_address(event.get("to"))
        elif event.get("topics"):
            topics = event.get("topics") or []
            if not topics or topics[0].lower() != TRANSFER_TOPIC:
                return None
            asset = self._asset_for_contract(event.get("address")) or ""
            if not asset:
                return None
            to_addr = self._normalize_hex_address(topics[2] if len(topics) > 2 else "")
        else:
            asset = "ETH"
            to_addr = self._normalize_hex_address(event.get("to"))

        user_id = self.address_user_map.get(to_addr.lower())
        if not user_id:
            return None

        amount = self._event_amount(event.get("value"), asset)
        if amount <= Decimal("0"):
            return None

        txid = event.get("txid") or event.get("transactionHash") or ""
        log_index = event.get("log_index")
        if log_index is None:
            log_index = self._hex_to_int(event.get("logIndex")) if event.get("logIndex") else 0
        unique_key = event.get("unique_key") or f"eth:{txid}:{asset}:{log_index}:{to_addr.lower()}"

        confirmations = int(event.get("confirmations", 0))
        finalized = bool(event.get("finalized", confirmations >= self.confirmations_required))
        return ChainDeposit(user_id=user_id, asset=asset, amount=amount, txid=txid, unique_key=unique_key, confirmations=confirmations, finalized=finalized)

    def fetch_deposits(self) -> list[ChainDeposit]:
        # External watcher pipelines should pass normalized events through ETH_DEPOSIT_EVENTS_JSON
        # or call this adapter with already fetched transfer/log data.
        raw_events = os.getenv("ETH_DEPOSIT_EVENTS_JSON", "[]")
        try:
            events = json.loads(raw_events)
            if not isinstance(events, list):
                return []
        except Exception:
            return []
        deposits: list[ChainDeposit] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            dep = self._parse_event(ev)
            if dep:
                deposits.append(dep)
        return deposits

    def broadcast_raw_transaction(self, asset: str, raw_tx_hex: str) -> str:
        resp = self._rpc("eth_sendRawTransaction", [raw_tx_hex])
        return resp.get("result", "")
