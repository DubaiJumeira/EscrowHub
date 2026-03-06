from __future__ import annotations

from infra.chain_adapters.xrp_rpc import XrpRpcAdapter


def run_once() -> int:
    # TODO: Implement validated payment scan with idempotency key txhash:destination_tag
    adapter = XrpRpcAdapter()
    _ = adapter.fetch_deposits()
    return 0
