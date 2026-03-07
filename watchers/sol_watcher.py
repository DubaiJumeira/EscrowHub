from __future__ import annotations

from infra.chain_adapters.sol_rpc import SolRpcAdapter


def run_once() -> int:
    # TODO: Implement finalized signature scan and idempotency key signature:instruction_index
    adapter = SolRpcAdapter()
    _ = adapter.fetch_deposits()
    return 0
